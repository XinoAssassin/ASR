//! The recorder thread: polls the capture sources, aligns them on the timeline and appends to
//! the partial WAV. Device loss never stops the recording: the track is silence-filled and the
//! device is reopened every few seconds on a helper thread (a slow open never stalls the loop).
//! A failing disk does not lose audio either: unwritten frames are kept (bounded) and retried.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use super::capture::{self, CaptureSource, Packet, SourceKind};
use super::timeline::{Timeline, TrackStats, OUT_RATE};
use super::wav::{Storage, WavWriter};

const POLL: Duration = Duration::from_millis(10);
const CHECKPOINT_S: f64 = 1.0;
const SYNC_S: f64 = 10.0;
const REOPEN_S: f64 = 2.0;
const DEFAULT_CHECK_S: f64 = 2.0;
pub const SILENCE_WARN_S: f64 = 60.0;
/// Audio kept in memory while the partial file cannot be written (disk full, share gone);
/// beyond this the session stops with an error instead of dropping audio.
pub const MAX_BACKLOG_S: f64 = 300.0;
/// `spawn` gives up when the devices are not open after this long.
pub const START_TIMEOUT: Duration = Duration::from_secs(20);

/// Platform services the recorder thread uses; tests substitute fakes.
pub trait Env: Send + Sync + 'static {
    /// Monotonic clock of the packet timestamps (seconds).
    fn now(&self) -> f64;
    /// Open a capture source (may block for seconds).
    fn open(
        &self,
        kind: SourceKind,
        device: Option<&str>,
        process: Option<u32>,
    ) -> Result<Box<dyn CaptureSource>, String>;
    fn default_device_id(&self, kind: SourceKind) -> Option<String>;
    fn process_exists(&self, pid: u32) -> bool;
    /// Per-thread initialisation, on every thread that opens or polls a source.
    fn init_thread(&self) {}
    /// Create the partial WAV (must not overwrite an existing file).
    fn create_wav(
        &self,
        path: &Path,
        channels: u16,
        rate: u32,
    ) -> std::io::Result<WavWriter<Box<dyn Storage>>>;
}

/// The real devices and files.
pub struct SystemEnv;

impl Env for SystemEnv {
    fn now(&self) -> f64 {
        capture::now()
    }
    fn open(
        &self,
        kind: SourceKind,
        device: Option<&str>,
        process: Option<u32>,
    ) -> Result<Box<dyn CaptureSource>, String> {
        capture::open(kind, device, process)
    }
    fn default_device_id(&self, kind: SourceKind) -> Option<String> {
        capture::default_device_id(kind)
    }
    fn process_exists(&self, pid: u32) -> bool {
        capture::process_exists(pid)
    }
    fn init_thread(&self) {
        capture::init_thread();
    }
    fn create_wav(
        &self,
        path: &Path,
        channels: u16,
        rate: u32,
    ) -> std::io::Result<WavWriter<Box<dyn Storage>>> {
        let f = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(path)?;
        let mut w = WavWriter::with_storage(Box::new(f) as Box<dyn Storage>, channels, rate)?;
        w.checkpoint(true)?;
        Ok(w)
    }
}

pub struct SessionConfig {
    pub system: bool,
    pub mic: bool,
    pub output_device: Option<String>,
    pub input_device: Option<String>,
    /// System audio of this process tree only (process loopback).
    pub process_id: Option<u32>,
    pub partial: PathBuf,
    /// Seconds of audio held in memory while the file cannot be written ([`MAX_BACKLOG_S`]).
    pub max_backlog_s: f64,
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
    /// Set when the session ended by itself (unrecoverable write failure); the caller should
    /// `stop` it so the partial file is finalised.
    pub fatal: Option<String>,
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
    /// Reopen attempt running on a helper thread.
    reopening: Option<mpsc::Receiver<Result<Box<dyn CaptureSource>, String>>>,
}

impl Slot {
    /// Start a reopen attempt on a helper thread (a blocking open must not stall the loop).
    fn reopen(&mut self, env: &Arc<dyn Env>) {
        let (tx, rx) = mpsc::channel();
        let (env, kind, device, process) =
            (env.clone(), self.kind, self.device.clone(), self.process);
        let spawned = std::thread::Builder::new()
            .name(format!("reopen-{kind:?}"))
            .spawn(move || {
                env.init_thread();
                // If the session ended meanwhile the receiver is gone and the source is dropped.
                let _ = tx.send(env.open(kind, device.as_deref(), process));
            });
        match spawned {
            Ok(_) => self.reopening = Some(rx),
            Err(e) => log::warn!("{kind:?}: cannot spawn reopen thread: {e}"),
        }
    }
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

const PENDING: u8 = 0;
const STARTED: u8 = 1;
const ABANDONED: u8 = 2;

/// Spawn the recorder thread. Returns once the devices are open, failed to open, or did not
/// open within `timeout` (then the thread is told to give up and never creates the file).
pub fn spawn(
    cfg: SessionConfig,
    live: Arc<Mutex<Live>>,
    stop: Arc<AtomicBool>,
    env: Arc<dyn Env>,
    timeout: Duration,
) -> Result<std::thread::JoinHandle<Result<SessionResult, String>>, String> {
    let (tx, rx) = mpsc::channel::<Result<(), String>>();
    let phase = Arc::new(AtomicU8::new(PENDING));
    let ph = phase.clone();
    let handle = std::thread::Builder::new()
        .name("recorder".into())
        .spawn(move || run(cfg, live, stop, env, ph, tx))
        .map_err(|e| e.to_string())?;
    let first = match rx.recv_timeout(timeout) {
        Err(mpsc::RecvTimeoutError::Timeout) => {
            if phase
                .compare_exchange(PENDING, ABANDONED, Ordering::AcqRel, Ordering::Acquire)
                .is_ok()
            {
                // The thread finds ABANDONED when its device opens return and exits without
                // creating the partial file; nobody waits for it.
                log::error!("audio devices did not open within {timeout:?}");
                return Err(format!(
                    "audio devices did not open within {} s",
                    timeout.as_secs()
                ));
            }
            // The devices opened just now: only the (quick) file creation remains.
            rx.recv().map_err(|_| ())
        }
        Err(mpsc::RecvTimeoutError::Disconnected) => Err(()),
        Ok(r) => Ok(r),
    };
    match first {
        Ok(Ok(())) => Ok(handle),
        Ok(Err(e)) => {
            let _ = handle.join();
            Err(e)
        }
        Err(()) => Err(match handle.join() {
            Ok(Err(e)) => e,
            _ => "recorder thread failed to start".into(),
        }),
    }
}

/// Packets polled from one slot in one loop iteration.
struct Polled {
    slot: usize,
    pkts: Vec<Packet>,
}

fn run(
    cfg: SessionConfig,
    live: Arc<Mutex<Live>>,
    stop: Arc<AtomicBool>,
    env: Arc<dyn Env>,
    phase: Arc<AtomicU8>,
    started: mpsc::Sender<Result<(), String>>,
) -> Result<SessionResult, String> {
    env.init_thread();
    let mut slots = Vec::new();
    let mut start_warnings = Vec::new();
    let mut errors = Vec::new();
    let kinds = [
        (
            cfg.system,
            SourceKind::System,
            capture::explicit_device(cfg.output_device.as_deref()).map(str::to_string),
            cfg.process_id,
        ),
        (
            cfg.mic,
            SourceKind::Mic,
            capture::explicit_device(cfg.input_device.as_deref()).map(str::to_string),
            None,
        ),
    ];
    for (on, kind, device, mut process) in kinds {
        if !on {
            continue;
        }
        if let Some(pid) = process.filter(|p| !env.process_exists(*p)) {
            log::warn!("process {pid} is gone; capturing the whole device");
            start_warnings.push(format!(
                "进程 {pid} 已不存在，改为录制整个输出设备 / process {pid} is gone, recording the whole output device"
            ));
            process = None;
        }
        match env.open(kind, device.as_deref(), process) {
            Ok(src) => slots.push(Slot {
                kind,
                device,
                process,
                opened_id: src.device_id(),
                rate: src.rate(),
                src: Some(src),
                lost_at: None,
                last_try: 0.0,
                reopening: None,
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
    if phase
        .compare_exchange(PENDING, STARTED, Ordering::AcqRel, Ordering::Acquire)
        .is_err()
    {
        // `spawn` timed out and returned an error: do not create a file nobody will finalise.
        log::warn!("start abandoned after the devices opened too late; closing them");
        return Err("start abandoned".into());
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
    let mut wav = match env.create_wav(&cfg.partial, channels, OUT_RATE as u32) {
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
    let t0 = env.now();
    let mut tl = Timeline::new(t0, &rates);
    let _ = started.send(Ok(()));
    log::info!("recording to {} ({channels} ch)", cfg.partial.display());

    let max_backlog = (cfg.max_backlog_s * OUT_RATE) as u64;
    let mut frames: Vec<i16> = Vec::new();
    let mut polled: Vec<Polled> = Vec::new();
    let mut last_checkpoint = t0;
    let mut last_sync = t0;
    let mut io_error: Option<String> = None;
    let mut fatal: Option<String> = None;
    let mut last_default_check = t0;
    let mut process_gone = false;
    let mut reported_skip = 0.0;
    let mut event_warnings: Vec<String> = Vec::new();
    loop {
        // Read the clock before polling: `tick(now)` silence-fills up to `now`, so every packet
        // captured before `now` must already be fed (a stall after this point is harmless).
        let now = env.now();
        // Follow a change of the Windows default device (only for "default" sources).
        if now - last_default_check >= DEFAULT_CHECK_S {
            last_default_check = now;
            process_gone = slots
                .iter()
                .any(|s| s.process.is_some_and(|p| !env.process_exists(p)));
            for s in slots.iter_mut() {
                if s.device.is_some() || s.process.is_some() || s.src.is_none() {
                    continue;
                }
                let current = env.default_device_id(s.kind);
                if current.is_some() && s.opened_id.is_some() && current != s.opened_id {
                    log::info!("{:?}: default device changed, switching", s.kind);
                    s.src = None;
                    s.lost_at = None;
                    s.last_try = f64::MIN; // reopen right away
                }
            }
        }
        let stopping = stop.load(Ordering::Acquire) || fatal.is_some();
        polled.clear();
        for (i, s) in slots.iter_mut().enumerate() {
            // A reopen attempt finished?
            if let Some(rx) = s.reopening.as_ref() {
                match rx.try_recv() {
                    Ok(Ok(src)) => {
                        log::info!("{:?} capture reopened", s.kind);
                        s.opened_id = src.device_id();
                        if src.rate() != s.rate {
                            s.rate = src.rate();
                            tl.reset_rate(i, s.rate);
                        }
                        s.src = Some(src);
                        s.lost_at = None;
                        s.reopening = None;
                    }
                    Ok(Err(e)) => {
                        log::debug!("{:?} reopen failed: {e}", s.kind);
                        s.reopening = None;
                        s.last_try = now;
                    }
                    Err(mpsc::TryRecvError::Empty) => {}
                    Err(mpsc::TryRecvError::Disconnected) => {
                        s.reopening = None;
                        s.last_try = now;
                    }
                }
            }
            match s.src.as_mut() {
                Some(src) => {
                    let mut p = Vec::new();
                    let res = src.poll(&mut p);
                    if !p.is_empty() {
                        polled.push(Polled { slot: i, pkts: p });
                    }
                    if let Err(e) = res {
                        log::warn!("{:?} capture lost: {e}", s.kind);
                        s.src = None;
                        if s.lost_at.is_none() {
                            s.lost_at = Some(now);
                        }
                        s.last_try = now;
                    }
                }
                None if !stopping && s.reopening.is_none() && now - s.last_try >= REOPEN_S => {
                    s.last_try = now;
                    s.reopen(&env);
                }
                None => {}
            }
        }
        for p in polled.drain(..) {
            for pk in &p.pkts {
                tl.feed(p.slot, pk.t, &pk.samples);
            }
        }
        if stopping {
            tl.finish(env.now());
        } else {
            tl.tick(now);
        }
        if tl.skipped_s() > reported_skip + 1.0 {
            let gap = tl.skipped_s() - reported_skip;
            reported_skip = tl.skipped_s();
            event_warnings.push(format!(
                "录音中断约 {} 秒（系统休眠？），该段已跳过 / recording paused for about {} s (system sleep?), gap skipped",
                gap.round() as u64,
                gap.round() as u64
            ));
        }
        frames.clear();
        tl.take_frames(&mut frames);
        if frames.capacity() > (1 << 20) && frames.capacity() > 4 * frames.len() {
            frames.shrink_to(1 << 16);
        }
        // `write` always keeps the frames (buffered until the disk takes them); while an error
        // persists, retry every iteration.
        let mut res = if frames.is_empty() {
            Ok(())
        } else {
            wav.write(&frames)
        };
        if res.is_ok() && (stopping || io_error.is_some() || now - last_checkpoint >= CHECKPOINT_S)
        {
            let sync = stopping || now - last_sync >= SYNC_S;
            res = wav.checkpoint(sync);
            if res.is_ok() {
                last_checkpoint = now;
                if sync {
                    last_sync = now;
                }
            }
        }
        match res {
            Ok(()) => {
                if let Some(e) = io_error.take() {
                    log::info!("recording file writable again (after: {e})");
                }
            }
            Err(e) => {
                if io_error.is_none() {
                    log::error!("write to {} failed: {e}", cfg.partial.display());
                }
                io_error = Some(e.to_string());
                if wav.pending_frames() > max_backlog && fatal.is_none() {
                    let msg = format!(
                        "录音文件持续写入失败，录音已停止 / could not write the recording for {} s, recording stopped: {e}",
                        cfg.max_backlog_s.round() as u64
                    );
                    log::error!("{msg}");
                    fatal = Some(msg);
                }
            }
        }
        {
            let peaks = tl.take_peaks();
            let mut l = live.lock().unwrap_or_else(|p| p.into_inner());
            l.elapsed = now - t0 - tl.skipped_s();
            l.frames = tl.written();
            l.drift_ms = tl.drift_ms();
            l.follower_ppm = tl.follower_ppm();
            l.warnings.clone_from(&start_warnings);
            l.warnings.extend(event_warnings.iter().cloned());
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
                l.warnings.push(format!(
                    "写入录音文件失败，正在重试（内存中暂存 {:.0} 秒）/ write failed, retrying ({:.0} s held in memory): {e}",
                    wav.pending_frames() as f64 / OUT_RATE,
                    wav.pending_frames() as f64 / OUT_RATE
                ));
            }
            if let Some(f) = &fatal {
                l.warnings.push(f.clone());
                l.fatal = Some(f.clone());
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
    // A last attempt for whatever is still buffered; what reached the disk stays a valid WAV
    // either way (the caller finalises the partial file).
    let finished = wav.finish();
    if let Some(f) = fatal {
        return Err(f);
    }
    let frames = finished.map_err(|e| format!("recording write error: {e}"))?;
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

#[cfg(test)]
mod tests {
    use super::super::testenv::FakeEnv;
    use super::*;

    fn cfg(partial: PathBuf, max_backlog_s: f64) -> SessionConfig {
        SessionConfig {
            system: true,
            mic: true,
            output_device: None,
            input_device: None,
            process_id: None,
            partial,
            max_backlog_s,
        }
    }

    type Handle = std::thread::JoinHandle<Result<SessionResult, String>>;

    fn start(
        env: &Arc<FakeEnv>,
        partial: PathBuf,
        backlog: f64,
    ) -> (Handle, Arc<Mutex<Live>>, Arc<AtomicBool>) {
        let live = Arc::new(Mutex::new(Live::default()));
        let stop = Arc::new(AtomicBool::new(false));
        let h = spawn(
            cfg(partial, backlog),
            live.clone(),
            stop.clone(),
            env.clone(),
            Duration::from_secs(5),
        )
        .unwrap();
        (h, live, stop)
    }

    /// A mic reopen that blocks for 0.6 s must not stall the loop: the healthy system track
    /// keeps every sample (no silence fill past its data, nothing dropped).
    #[test]
    fn slow_reopen_does_not_drop_healthy_track() {
        let dir = tempfile::tempdir().unwrap();
        let env = FakeEnv::new();
        *env.mic_dies_after.lock().unwrap() = Some(0.2);
        *env.reopen_delay.lock().unwrap() = Duration::from_millis(600);
        let (h, live, stop) = start(&env, dir.path().join("a.partial.wav"), 60.0);
        // loss at 0.2 s, reopen attempt at 2.2 s that takes until 2.8 s
        std::thread::sleep(Duration::from_millis(3300));
        assert_eq!(env.opens(SourceKind::Mic), 2, "mic was reopened");
        let warnings = live.lock().unwrap().warnings.clone();
        assert!(warnings.is_empty(), "{warnings:?}");
        stop.store(true, Ordering::Release);
        let r = h.join().unwrap().unwrap();
        let (sys, mic) = (&r.stats[0], &r.stats[1]);
        assert!(sys.dropped_s < 0.002, "{sys:?}");
        assert!(sys.silence_inserted_s < 0.05, "{sys:?}");
        // the mic gap (~2.6 s) was silence-filled
        assert!(mic.silence_inserted_s > 2.0, "{mic:?}");
        // 3.3 s recorded
        assert!(
            (r.frames as f64 / OUT_RATE - 3.3).abs() < 0.2,
            "{}",
            r.frames
        );
    }

    /// Writes fail for half a second: frames are held and written later, none lost.
    #[test]
    fn transient_write_errors_lose_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("w.partial.wav");
        let env = FakeEnv::new();
        let (h, live, stop) = start(&env, p.clone(), 60.0);
        std::thread::sleep(Duration::from_millis(300));
        env.fail_writes.store(true, Ordering::SeqCst);
        std::thread::sleep(Duration::from_millis(1500));
        let during = live.lock().unwrap().warnings.clone();
        assert!(
            during.iter().any(|w| w.contains("write failed")),
            "{during:?}"
        );
        env.fail_writes.store(false, Ordering::SeqCst);
        std::thread::sleep(Duration::from_millis(500));
        assert!(live.lock().unwrap().warnings.is_empty());
        stop.store(true, Ordering::Release);
        let r = h.join().unwrap().unwrap();
        let produced = live.lock().unwrap().frames;
        assert_eq!(r.frames, produced);
        assert!(
            (r.frames as f64 / OUT_RATE - 2.3).abs() < 0.2,
            "{}",
            r.frames
        );
        let len = std::fs::metadata(&p).unwrap().len();
        assert_eq!(len, 44 + r.frames * 4);
        assert_eq!(
            super::super::wav::WavReader::open(&p).unwrap().info.frames,
            r.frames
        );
    }

    /// The disk never comes back: once the backlog is full the session stops by itself with
    /// a fatal error (it never keeps "recording" while dropping audio).
    #[test]
    fn backlog_overflow_stops_the_session() {
        let dir = tempfile::tempdir().unwrap();
        let env = FakeEnv::new();
        let (h, live, _stop) = start(&env, dir.path().join("f.partial.wav"), 0.3);
        std::thread::sleep(Duration::from_millis(200));
        env.fail_writes.store(true, Ordering::SeqCst);
        let t = std::time::Instant::now();
        while !h.is_finished() && t.elapsed() < Duration::from_secs(5) {
            std::thread::sleep(Duration::from_millis(20));
        }
        assert!(h.is_finished());
        let fatal = live.lock().unwrap().fatal.clone().expect("fatal error set");
        assert!(fatal.contains("recording stopped"), "{fatal}");
        assert!(live.lock().unwrap().warnings.contains(&fatal));
        assert_eq!(h.join().unwrap().unwrap_err(), fatal);
    }

    /// Devices that take longer than the start timeout: `spawn` fails, and the thread never
    /// creates the partial file when the opens finally return.
    #[test]
    fn start_times_out_without_leaving_a_file() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("t.partial.wav");
        let env = FakeEnv::new();
        *env.open_delay.lock().unwrap() = Duration::from_millis(400);
        let t = std::time::Instant::now();
        let res = spawn(
            cfg(p.clone(), 60.0),
            Arc::new(Mutex::new(Live::default())),
            Arc::new(AtomicBool::new(false)),
            env.clone(),
            Duration::from_millis(100),
        );
        assert!(res.unwrap_err().contains("did not open"));
        assert!(t.elapsed() < Duration::from_millis(300));
        std::thread::sleep(Duration::from_millis(1200));
        assert_eq!(env.opens(SourceKind::Mic), 1, "both opens ran");
        assert!(!p.exists());
    }
}
