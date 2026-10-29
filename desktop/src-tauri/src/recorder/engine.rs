//! The recorder thread: polls the capture sources, aligns them on the timeline and appends to
//! the partial WAV. Device loss never stops the recording: the track is silence-filled and the
//! device is reopened every few seconds.

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use super::capture::{self, CaptureSource, Packet, SourceKind};
use super::timeline::{Timeline, TrackStats, OUT_RATE};
use super::wav::WavWriter;

const POLL: Duration = Duration::from_millis(10);
const CHECKPOINT_S: f64 = 1.0;
const SYNC_S: f64 = 10.0;
const REOPEN_S: f64 = 2.0;
const DEFAULT_CHECK_S: f64 = 2.0;
pub const SILENCE_WARN_S: f64 = 60.0;

pub struct SessionConfig {
    pub system: bool,
    pub mic: bool,
    pub output_device: Option<String>,
    pub input_device: Option<String>,
    /// System audio of this process tree only (process loopback).
    pub process_id: Option<u32>,
    pub partial: PathBuf,
}

/// Live values shared with the UI side.
#[derive(Debug, Default, Clone)]
pub struct Live {
    pub elapsed: f64,
    pub peak_system: f32,
    pub peak_mic: f32,
    pub drift_ms: Option<f64>,
    pub follower_ppm: Option<f64>,
    pub warnings: Vec<String>,
    pub frames: u64,
    /// Sources actually captured.
    pub sources: crate::ipc::Sources,
    pub process_id: Option<u32>,
}

#[derive(Debug, Clone)]
pub struct SessionResult {
    pub frames: u64,
    pub channels: u16,
    pub stats: Vec<TrackStats>,
    pub reference_rate: f64,
}

struct Slot {
    kind: SourceKind,
    device: Option<String>,
    process: Option<u32>,
    /// Endpoint id actually opened (to notice default-device changes).
    opened_id: Option<String>,
    src: Option<Box<dyn CaptureSource>>,
    rate: f64,
    lost_at: Option<f64>,
    last_try: f64,
}

fn label(kind: SourceKind) -> &'static str {
    match kind {
        SourceKind::System => "系统音频",
        SourceKind::Mic => "麦克风",
    }
}

fn label_en(kind: SourceKind) -> &'static str {
    match kind {
        SourceKind::System => "system audio",
        SourceKind::Mic => "microphone",
    }
}

/// Spawn the recorder thread. Returns once the devices are open (or failed to open).
pub fn spawn(
    cfg: SessionConfig,
    live: Arc<Mutex<Live>>,
    stop: Arc<AtomicBool>,
) -> Result<std::thread::JoinHandle<Result<SessionResult, String>>, String> {
    let (tx, rx) = mpsc::channel::<Result<(), String>>();
    let handle = std::thread::Builder::new()
        .name("recorder".into())
        .spawn(move || run(cfg, live, stop, tx))
        .map_err(|e| e.to_string())?;
    match rx.recv() {
        Ok(Ok(())) => Ok(handle),
        Ok(Err(e)) => {
            let _ = handle.join();
            Err(e)
        }
        Err(_) => Err(match handle.join() {
            Ok(Err(e)) => e,
            _ => "recorder thread failed to start".into(),
        }),
    }
}

fn run(
    cfg: SessionConfig,
    live: Arc<Mutex<Live>>,
    stop: Arc<AtomicBool>,
    started: mpsc::Sender<Result<(), String>>,
) -> Result<SessionResult, String> {
    capture::init_thread();
    let mut slots = Vec::new();
    let mut start_warnings = Vec::new();
    let mut errors = Vec::new();
    let kinds = [
        (
            cfg.system,
            SourceKind::System,
            cfg.output_device.clone(),
            cfg.process_id,
        ),
        (cfg.mic, SourceKind::Mic, cfg.input_device.clone(), None),
    ];
    for (on, kind, device, mut process) in kinds {
        if !on {
            continue;
        }
        if let Some(pid) = process.filter(|p| !capture::process_exists(*p)) {
            log::warn!("process {pid} is gone; capturing the whole device");
            start_warnings.push(format!(
                "进程 {pid} 已不存在，改为录制整个输出设备 / process {pid} is gone, recording the whole output device"
            ));
            process = None;
        }
        match capture::open(kind, device.as_deref(), process) {
            Ok(src) => slots.push(Slot {
                kind,
                device,
                process,
                opened_id: src.device_id(),
                rate: src.rate(),
                src: Some(src),
                lost_at: None,
                last_try: 0.0,
            }),
            Err(e) => {
                log::warn!("{kind:?} unavailable: {e}");
                errors.push(format!("{}: {e}", label_en(kind)));
                start_warnings.push(format!(
                    "{}不可用，本次未录制 / {} unavailable, not recorded: {e}",
                    label(kind),
                    label_en(kind)
                ));
            }
        }
    }
    if slots.is_empty() {
        let msg = if errors.is_empty() {
            "no audio source enabled".to_string()
        } else {
            format!("no audio source could be opened ({})", errors.join("; "))
        };
        let _ = started.send(Err(msg.clone()));
        return Err(msg);
    }
    {
        let mut l = live.lock().unwrap_or_else(|p| p.into_inner());
        l.sources = crate::ipc::Sources {
            system: slots.iter().any(|s| s.kind == SourceKind::System),
            mic: slots.iter().any(|s| s.kind == SourceKind::Mic),
        };
        l.process_id = slots
            .iter()
            .find(|s| s.kind == SourceKind::System)
            .and_then(|s| s.process);
        l.warnings = start_warnings.clone();
    }
    let channels = slots.len() as u16;
    let mut wav = match WavWriter::create(&cfg.partial, channels, OUT_RATE as u32) {
        Ok(w) => w,
        Err(e) => {
            let msg = format!("cannot create {}: {e}", cfg.partial.display());
            let _ = started.send(Err(msg.clone()));
            return Err(msg);
        }
    };
    // Test hook: pretend the mic clock is off by N ppm (exercises the drift controller on
    // machines whose devices share one clock). Never set in normal use.
    let skew = std::env::var("ASR_RECORDER_TEST_MIC_SKEW_PPM")
        .ok()
        .and_then(|v| v.parse::<f64>().ok())
        .unwrap_or(0.0);
    let rates: Vec<f64> = slots
        .iter()
        .map(|s| match s.kind {
            SourceKind::Mic if skew != 0.0 => {
                log::warn!("test hook: mic nominal rate skewed by {skew} ppm");
                s.rate * (1.0 + skew * 1e-6)
            }
            _ => s.rate,
        })
        .collect();
    // Drop whatever the devices buffered before the start.
    let mut pkts: Vec<Packet> = Vec::new();
    for s in &mut slots {
        if let Some(src) = s.src.as_mut() {
            let _ = src.poll(&mut pkts);
        }
    }
    pkts.clear();
    let t0 = capture::now();
    let mut tl = Timeline::new(t0, &rates);
    let _ = started.send(Ok(()));
    log::info!("recording to {} ({channels} ch)", cfg.partial.display());

    let mut frames: Vec<i16> = Vec::new();
    let mut last_checkpoint = t0;
    let mut last_sync = t0;
    let mut io_error: Option<String> = None;
    let mut last_default_check = t0;
    let mut process_gone = false;
    loop {
        // Follow a change of the Windows default device (only for "default" sources).
        if capture::now() - last_default_check >= DEFAULT_CHECK_S {
            last_default_check = capture::now();
            process_gone = slots
                .iter()
                .any(|s| s.process.is_some_and(|p| !capture::process_exists(p)));
            for s in slots.iter_mut() {
                if s.device.is_some() || s.process.is_some() || s.src.is_none() {
                    continue;
                }
                let current = capture::default_device_id(s.kind);
                if current.is_some() && s.opened_id.is_some() && current != s.opened_id {
                    log::info!("{:?}: default device changed, switching", s.kind);
                    s.src = None;
                    s.lost_at = None;
                    s.last_try = f64::MIN; // reopen right away
                }
            }
        }
        let stopping = stop.load(Ordering::Acquire);
        for (i, s) in slots.iter_mut().enumerate() {
            let now = capture::now();
            match s.src.as_mut() {
                Some(src) => {
                    pkts.clear();
                    let res = src.poll(&mut pkts);
                    for p in &pkts {
                        tl.feed(i, p.t, &p.samples);
                    }
                    if let Err(e) = res {
                        log::warn!("{:?} capture lost: {e}", s.kind);
                        s.src = None;
                        s.lost_at = Some(now);
                        s.last_try = now;
                    }
                }
                None if !stopping && now - s.last_try >= REOPEN_S => {
                    s.last_try = now;
                    match capture::open(s.kind, s.device.as_deref(), s.process) {
                        Ok(src) => {
                            log::info!("{:?} capture reopened", s.kind);
                            s.opened_id = src.device_id();
                            if src.rate() != s.rate {
                                s.rate = src.rate();
                                tl.reset_rate(i, s.rate);
                            }
                            s.src = Some(src);
                            s.lost_at = None;
                        }
                        Err(e) => log::debug!("{:?} reopen failed: {e}", s.kind),
                    }
                }
                None => {}
            }
        }
        let now = capture::now();
        if stopping {
            tl.finish(now);
        } else {
            tl.tick(now);
        }
        frames.clear();
        tl.take_frames(&mut frames);
        if io_error.is_none() && !frames.is_empty() {
            if let Err(e) = wav.write(&frames) {
                io_error = Some(e.to_string());
            }
        }
        if io_error.is_none() && (stopping || now - last_checkpoint >= CHECKPOINT_S) {
            let sync = now - last_sync >= SYNC_S;
            if let Err(e) = wav.checkpoint(sync) {
                io_error = Some(e.to_string());
            }
            last_checkpoint = now;
            if sync {
                last_sync = now;
            }
        }
        {
            let peaks = tl.take_peaks();
            let mut l = live.lock().unwrap_or_else(|p| p.into_inner());
            l.elapsed = now - t0;
            l.frames = tl.written();
            l.drift_ms = tl.drift_ms();
            l.follower_ppm = tl.follower_ppm();
            l.warnings.clone_from(&start_warnings);
            for (i, s) in slots.iter().enumerate() {
                if let Some(pid) = s.process.filter(|_| process_gone) {
                    l.warnings.push(format!(
                        "被录制的进程 {pid} 已退出 / captured process {pid} has exited"
                    ));
                }
                let p = peaks[i];
                match s.kind {
                    SourceKind::System => l.peak_system = l.peak_system.max(p),
                    SourceKind::Mic => l.peak_mic = l.peak_mic.max(p),
                }
                if s.lost_at.is_some() {
                    l.warnings.push(format!(
                        "{}设备已断开，正在重新连接（期间以静音填充）",
                        label(s.kind)
                    ));
                } else {
                    let silent = tl.silent_for(i, now);
                    if silent >= SILENCE_WARN_S {
                        l.warnings.push(format!(
                            "{}已静音 {} 秒 / {} silent for {} s",
                            label(s.kind),
                            silent as u64,
                            label_en(s.kind),
                            silent as u64
                        ));
                    }
                }
            }
            if let Some(e) = &io_error {
                l.warnings
                    .push(format!("写入录音文件失败 / write failed: {e}"));
            }
        }
        if stopping {
            break;
        }
        std::thread::sleep(POLL);
    }
    let stats = tl.stats();
    let reference_rate = tl.map.rate();
    drop(slots);
    if let Some(e) = io_error {
        // Keep whatever reached the disk; the caller still finalises the partial file.
        let _ = wav.checkpoint(true);
        log::error!("recording write error: {e}");
    }
    let frames = wav.finish().map_err(|e| e.to_string())?;
    log::info!(
        "recording stopped: {frames} frames, reference clock ratio {reference_rate:.6}, stats {stats:?}"
    );
    Ok(SessionResult {
        frames,
        channels,
        stats,
        reference_rate,
    })
}
