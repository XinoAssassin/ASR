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
pub const SILENCE_WARN_S: f64 = 60.0;

pub struct SessionConfig {
    pub system: bool,
    pub mic: bool,
    pub output_device: Option<String>,
    pub input_device: Option<String>,
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
    let kinds = [
        (cfg.system, SourceKind::System, cfg.output_device.clone()),
        (cfg.mic, SourceKind::Mic, cfg.input_device.clone()),
    ];
    for (on, kind, device) in kinds {
        if !on {
            continue;
        }
        match capture::open(kind, device.as_deref()) {
            Ok(src) => slots.push(Slot {
                kind,
                device,
                rate: src.rate(),
                src: Some(src),
                lost_at: None,
                last_try: 0.0,
            }),
            Err(e) => {
                let msg = format!("{}无法打开: {e}", label(kind));
                let _ = started.send(Err(msg.clone()));
                return Err(msg);
            }
        }
    }
    if slots.is_empty() {
        let msg = "no audio source enabled".to_string();
        let _ = started.send(Err(msg.clone()));
        return Err(msg);
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
    loop {
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
                    match capture::open(s.kind, s.device.as_deref()) {
                        Ok(src) => {
                            log::info!("{:?} capture reopened", s.kind);
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
            l.warnings.clear();
            for (i, s) in slots.iter().enumerate() {
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
                            match s.kind {
                                SourceKind::System => "system audio",
                                SourceKind::Mic => "microphone",
                            },
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
