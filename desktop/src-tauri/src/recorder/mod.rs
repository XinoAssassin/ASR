//! Recorder: capture -> timeline -> `<stem>.partial.wav` -> verified `<stem>.flac`.

pub mod capture;
pub mod engine;
pub mod flac;
#[cfg(target_os = "linux")]
mod pulse_capture;
pub mod resample;
#[cfg(target_os = "macos")]
mod sck_capture;
#[cfg(test)]
mod testenv;
pub mod timeline;
#[cfg(windows)]
mod wasapi_capture;
pub mod wav;

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::SystemTime;

use chrono::{DateTime, Local, Utc};

use crate::ipc::{Levels, RecorderState, RecorderStatus, StartOptions};
use engine::{Env, Live, SessionConfig, SessionResult, SystemEnv};

pub const PARTIAL_SUFFIX: &str = ".partial.wav";

/// [`Recorder::failure`] of a recorder thread that ended without a reason (e.g. a panic).
pub const THREAD_ENDED: &str =
    "录音线程异常退出，录音已停止 / recorder thread ended unexpectedly, recording stopped";

/// A finished recording file on disk.
#[derive(Debug, Clone)]
pub struct Finished {
    pub path: PathBuf,
    pub stem: String,
    pub size: u64,
    pub duration: f64,
    pub channels: u16,
    pub created_at: DateTime<Utc>,
}

/// A recording found by [`Recorder::recover_dir`].
#[derive(Debug, Clone)]
pub struct Recovered {
    pub finished: Finished,
    /// false: an orphaned partial was finalised just now. true: an already finished
    /// `<stem>.flac` / `<stem>.wav` from an earlier run (a crash may have hit between finalising
    /// and queueing it); the caller adds it only if its path is not indexed yet.
    pub adopted: bool,
}

struct Session {
    thread: JoinHandle<Result<SessionResult, String>>,
    stop: Arc<AtomicBool>,
    live: Arc<Mutex<Live>>,
    partial: PathBuf,
    stem: String,
    created_at: DateTime<Utc>,
}

struct Inner {
    state: RecorderState,
    session: Option<Session>,
    /// A `start` is opening devices (the lock is not held meanwhile).
    starting: bool,
    last_file: Option<String>,
    /// Peak levels already reported by `status(true)` are cleared here.
    last_status: Option<RecorderStatus>,
    /// Stems whose files are owned by a starting / recording / finalising session or by a
    /// running recovery. Recovery never touches them, `start` never picks them.
    busy: HashSet<String>,
}

pub struct Recorder {
    inner: Mutex<Inner>,
    env: Arc<dyn Env>,
    max_backlog_s: f64,
    /// Finished files older than this can be leftovers of an earlier run (see `Recovered`).
    created: SystemTime,
}

impl Default for Recorder {
    fn default() -> Self {
        Self::new()
    }
}

const FILE_SUFFIXES: [&str; 4] = [".flac", PARTIAL_SUFFIX, ".wav", ".flac.part"];

/// Pick `<dir>/<stem>` with a stem that clashes with no existing recording file.
pub fn unique_stem(dir: &Path, base: &str) -> String {
    unique_stem_except(dir, base, &HashSet::new())
}

fn unique_stem_except(dir: &Path, base: &str, busy: &HashSet<String>) -> String {
    let taken = |s: &str| {
        busy.contains(s)
            || FILE_SUFFIXES
                .iter()
                .any(|ext| dir.join(format!("{s}{ext}")).exists())
    };
    if !taken(base) {
        return base.to_string();
    }
    (2..)
        .map(|i| format!("{base}_{i}"))
        .find(|s| !taken(s))
        .unwrap()
}

/// Whether `stem` looks like one the recorder makes (`YYYY-MM-DD_HH-MM-SS` + optional `_N`).
fn is_recording_stem(stem: &str) -> bool {
    let b = stem.as_bytes();
    if b.len() < 19 {
        return false;
    }
    let shape = b[..19].iter().enumerate().all(|(i, &c)| match i {
        4 | 7 => c == b'-',
        10 => c == b'_',
        13 | 16 => c == b'-',
        _ => c.is_ascii_digit(),
    });
    let rest = &stem[19..];
    shape
        && (rest.is_empty()
            || rest
                .strip_prefix('_')
                .is_some_and(|n| !n.is_empty() && n.bytes().all(|c| c.is_ascii_digit())))
}

impl Recorder {
    pub fn new() -> Self {
        Self::with_env(Arc::new(SystemEnv))
    }

    /// A recorder on custom platform services (tests).
    pub fn with_env(env: Arc<dyn Env>) -> Self {
        Self {
            inner: Mutex::new(Inner {
                state: RecorderState::Idle,
                session: None,
                starting: false,
                last_file: None,
                last_status: None,
                busy: HashSet::new(),
            }),
            env,
            max_backlog_s: engine::MAX_BACKLOG_S,
            created: SystemTime::now(),
        }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(|p| p.into_inner())
    }

    pub fn state(&self) -> RecorderState {
        self.lock().state
    }

    /// Path of the partial WAV being written, if recording.
    pub fn active_partial(&self) -> Option<PathBuf> {
        self.lock().session.as_ref().map(|s| s.partial.clone())
    }

    /// Some(reason) once the session ended by itself: the file could not be written for
    /// [`engine::MAX_BACKLOG_S`], or the recorder thread died (panic in a capture backend or
    /// an early return) without saying why. The state stays `Recording` until
    /// [`stop`](Self::stop) is called, which finalises the partial file as usual.
    pub fn failure(&self) -> Option<String> {
        let inner = self.lock();
        let s = inner.session.as_ref()?;
        let fatal = s
            .live
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .fatal
            .clone();
        fatal.or_else(|| s.thread.is_finished().then(|| THREAD_ENDED.to_string()))
    }

    /// Start recording. Blocks while the devices open (bounded by [`engine::START_TIMEOUT`]),
    /// without holding the recorder lock: `status` / `recover_dir` keep working meanwhile.
    pub fn start(&self, opts: &StartOptions, out_dir: &Path) -> Result<RecorderStatus, String> {
        if !opts.system && !opts.mic {
            return Err("enable system audio and/or microphone".into());
        }
        std::fs::create_dir_all(out_dir)
            .map_err(|e| format!("cannot create {}: {e}", out_dir.display()))?;
        let (stem, partial, now) = {
            let mut inner = self.lock();
            if inner.state != RecorderState::Idle || inner.starting {
                return Err("already recording".into());
            }
            let now = Local::now();
            let stem = unique_stem_except(out_dir, &crate::util::recording_stem(now), &inner.busy);
            // Reserved before the file exists, so recovery can never see it unowned.
            inner.busy.insert(stem.clone());
            inner.starting = true;
            let partial = out_dir.join(format!("{stem}{PARTIAL_SUFFIX}"));
            (stem, partial, now)
        };
        let live = Arc::new(Mutex::new(Live::default()));
        let stop = Arc::new(AtomicBool::new(false));
        let res = engine::spawn(
            SessionConfig {
                system: opts.system,
                mic: opts.mic,
                output_device: opts.output_device.clone(),
                input_device: opts.input_device.clone(),
                process_id: opts.process_id.filter(|_| opts.system),
                partial: partial.clone(),
                max_backlog_s: self.max_backlog_s,
            },
            live.clone(),
            stop.clone(),
            self.env.clone(),
            engine::START_TIMEOUT,
        );
        let mut inner = self.lock();
        inner.starting = false;
        let thread = match res {
            Ok(t) => t,
            Err(e) => {
                // No file was created (or the thread is gone): release the reservation.
                inner.busy.remove(&stem);
                return Err(e);
            }
        };
        inner.session = Some(Session {
            thread,
            stop,
            live,
            partial: partial.clone(),
            stem,
            created_at: now.with_timezone(&Utc),
        });
        inner.state = RecorderState::Recording;
        inner.last_file = Some(partial.to_string_lossy().into_owned());
        Ok(status_of(&inner, false))
    }

    /// Current status. `take_peaks` resets the peak meters (the 10 Hz event does this).
    pub fn status(&self, take_peaks: bool) -> RecorderStatus {
        let mut inner = self.lock();
        let st = status_of(&inner, take_peaks);
        inner.last_status = Some(st.clone());
        st
    }

    /// Stop, wait for the recorder thread and finalise the FLAC. Blocks (FLAC encode).
    pub fn stop(&self) -> Result<Finished, String> {
        let session = {
            let mut inner = self.lock();
            if inner.state != RecorderState::Recording {
                return Err("not recording".into());
            }
            // The stem stays busy until the partial is finalised.
            let Some(session) = inner.session.take() else {
                // Cannot happen (Recording always has a session); recover instead of panicking.
                log::error!("recorder state Recording without a session; resetting to idle");
                inner.state = RecorderState::Idle;
                return Err("not recording".into());
            };
            inner.state = RecorderState::Finalizing;
            session
        };
        session.stop.store(true, Ordering::Release);
        let result = session
            .thread
            .join()
            .map_err(|p| format!("recorder thread panicked: {}", panic_message(&*p)))
            .and_then(|r| r);
        if let Err(e) = &result {
            log::error!("recorder thread ended with: {e}");
        }
        // Whatever happened, the partial file (if any) is finalised, never discarded.
        let finished = finalize(&session.partial, &session.stem, session.created_at);
        let mut inner = self.lock();
        inner.busy.remove(&session.stem);
        inner.state = RecorderState::Idle;
        match &finished {
            Ok(f) => inner.last_file = Some(f.path.to_string_lossy().into_owned()),
            Err(e) => log::error!("finalise failed: {e}"),
        }
        finished
    }

    /// Claim `stem` for recovery: false if a session (or another recovery) owns it.
    fn claim(&self, stem: &str) -> bool {
        self.lock().busy.insert(stem.to_string())
    }

    fn release(&self, stem: &str) {
        self.lock().busy.remove(stem);
    }

    /// Crash recovery of `dir`, safe to run while recordings start and stop:
    /// * every orphaned `<stem>.partial.wav` is finalised (`adopted == false`), except the files
    ///   of a starting / recording / finalising session, checked per file under the recorder
    ///   lock (stems are matched by name, so a same-named partial in another directory is just
    ///   left for the next run);
    /// * every finished `<stem>.flac` / `<stem>.wav` with a recorder stem, no partial and a
    ///   modification time before this `Recorder` was created is returned with `adopted ==
    ///   true` (it may be missing from the upload queue after a crash).
    ///
    /// Blocks (FLAC encode of each orphan); run it on a worker thread. Gating `start` on it is
    /// not needed.
    pub fn recover_dir(&self, dir: &Path) -> Vec<Recovered> {
        let mut out = Vec::new();
        let mut done = HashSet::new();
        for (p, stem, created) in list_dir(dir, |n| n.strip_suffix(PARTIAL_SUFFIX)) {
            if !self.claim(&stem) {
                log::info!("recovery: {} belongs to a session, skipped", p.display());
                continue;
            }
            // A session may have finalised it between the listing and the claim.
            if p.exists() {
                match guarded(|| finalize(&p, &stem, created)) {
                    Ok(f) => {
                        log::info!("recovered {}", f.path.display());
                        done.insert(stem.clone());
                        out.push(Recovered {
                            finished: f,
                            adopted: false,
                        });
                    }
                    Err(e) => log::error!("recovery of {} failed: {e}", p.display()),
                }
            }
            self.release(&stem);
        }
        for (p, stem, created) in list_dir(dir, finished_stem) {
            if done.contains(&stem) || dir.join(format!("{stem}{PARTIAL_SUFFIX}")).exists() {
                continue;
            }
            let old = std::fs::metadata(&p)
                .and_then(|m| m.modified())
                .is_ok_and(|m| m < self.created);
            if !old || !self.claim(&stem) {
                continue;
            }
            match guarded(|| describe(&p, &stem, created)) {
                Ok(f) => out.push(Recovered {
                    finished: f,
                    adopted: true,
                }),
                Err(e) => log::warn!("cannot read {}: {e}", p.display()),
            }
            self.release(&stem);
        }
        out
    }
}

/// Run `f`, turning a panic (third-party decoders / encoders on arbitrary leftover files) into
/// an error, so one bad file neither ends recovery nor leaves its stem claimed.
fn guarded<T>(f: impl FnOnce() -> Result<T, String>) -> Result<T, String> {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(f))
        .unwrap_or_else(|p| Err(format!("panicked: {}", panic_message(&*p))))
}

/// Stem of a finished recording file name (`<stem>.flac` / `<stem>.wav`, recorder stems only).
fn finished_stem(name: &str) -> Option<&str> {
    name.strip_suffix(".flac")
        .or_else(|| name.strip_suffix(".wav"))
        .filter(|s| is_recording_stem(s))
}

/// `(path, stem, created)` of the files in `dir` whose name `stem_of` accepts.
fn list_dir(
    dir: &Path,
    stem_of: impl for<'a> Fn(&'a str) -> Option<&'a str>,
) -> Vec<(PathBuf, String, DateTime<Utc>)> {
    let Ok(rd) = std::fs::read_dir(dir) else {
        return Vec::new();
    };
    let mut v: Vec<_> = rd
        .flatten()
        .filter_map(|e| {
            let name = e.file_name();
            let stem = stem_of(name.to_str()?)?.to_string();
            let created = e
                .metadata()
                .and_then(|m| m.created().or_else(|_| m.modified()))
                .map(DateTime::<Utc>::from)
                .unwrap_or_else(|_| Utc::now());
            Some((e.path(), stem, created))
        })
        .collect();
    v.sort();
    v
}

/// `Finished` for an existing FLAC / WAV recording.
fn describe(path: &Path, stem: &str, created_at: DateTime<Utc>) -> Result<Finished, String> {
    let (frames, rate, channels) = if path.extension().is_some_and(|e| e == "flac") {
        let r = claxon::FlacReader::open(path).map_err(|e| e.to_string())?;
        let si = r.streaminfo();
        (si.samples.unwrap_or(0), si.sample_rate, si.channels as u16)
    } else {
        let r = wav::WavReader::open(path).map_err(|e| e.to_string())?;
        (r.info.frames, r.info.rate, r.info.channels)
    };
    Ok(Finished {
        path: path.to_path_buf(),
        stem: stem.to_string(),
        size: std::fs::metadata(path).map(|m| m.len()).unwrap_or(0),
        duration: frames as f64 / rate.max(1) as f64,
        channels,
        created_at,
    })
}

fn panic_message(p: &(dyn std::any::Any + Send)) -> &str {
    p.downcast_ref::<&str>()
        .copied()
        .or_else(|| p.downcast_ref::<String>().map(String::as_str))
        .unwrap_or("(no message)")
}

fn status_of(inner: &Inner, take_peaks: bool) -> RecorderStatus {
    let Some(s) = inner.session.as_ref() else {
        let mut st = RecorderStatus::idle(inner.last_file.clone());
        st.state = inner.state;
        return st;
    };
    let mut l = s.live.lock().unwrap_or_else(|p| p.into_inner());
    let st = RecorderStatus {
        state: inner.state,
        elapsed: (l.elapsed * 10.0).round() / 10.0,
        levels: Levels {
            system: l.peak_system,
            mic: l.peak_mic,
        },
        file: Some(s.partial.to_string_lossy().into_owned()),
        drift_ms: l.drift_ms,
        warnings: l.warnings.clone(),
        sources: l.sources,
        process_id: l.process_id,
    };
    if take_peaks {
        l.peak_system = 0.0;
        l.peak_mic = 0.0;
    }
    st
}

/// Turn `<dir>/<stem>.partial.wav` into a verified `<dir>/<stem>.flac`, then delete the partial.
/// Also used for crash recovery (the WAV header is re-derived from the file length first).
/// If FLAC fails, the WAV is kept as `<stem>.wav` and returned instead (never deleted).
pub fn finalize(partial: &Path, stem: &str, created_at: DateTime<Utc>) -> Result<Finished, String> {
    finalize_with(partial, stem, created_at, |wav, flac| {
        flac::encode(wav, flac)?;
        flac::verify(flac, wav)
    })
}

/// `finalize` with the encode + verify step (returning the verified frame count) injected.
fn finalize_with(
    partial: &Path,
    stem: &str,
    created_at: DateTime<Utc>,
    encode_verify: impl FnOnce(&Path, &Path) -> std::io::Result<u64>,
) -> Result<Finished, String> {
    let dir = partial.parent().unwrap_or(Path::new("."));
    let info = wav::repair(partial).map_err(|e| format!("{}: {e}", partial.display()))?;
    let flac_path = dir.join(format!("{stem}.flac"));
    let tmp = dir.join(format!("{stem}.flac.part"));
    let res = (|| -> std::io::Result<()> {
        if flac_path.exists() {
            // A crash between the rename below and the removal of the partial: if that FLAC
            // holds exactly this recording, the job is done (keeping both would queue the
            // recording twice). Anything else keeps both files.
            return match flac::verify(&flac_path, partial) {
                Ok(n) if n == info.frames => {
                    log::info!("{} is already complete", flac_path.display());
                    Ok(())
                }
                Ok(n) => Err(std::io::Error::new(
                    std::io::ErrorKind::AlreadyExists,
                    format!(
                        "{} exists with {n} of {} frames",
                        flac_path.display(),
                        info.frames
                    ),
                )),
                Err(e) => Err(std::io::Error::new(
                    std::io::ErrorKind::AlreadyExists,
                    format!("{} exists and differs: {e}", flac_path.display()),
                )),
            };
        }
        let verified = encode_verify(partial, &tmp)?;
        // The partial is only deleted when the FLAC holds every frame the file has on disk.
        if verified != info.frames {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                format!(
                    "FLAC holds {verified} frames, the recording {}",
                    info.frames
                ),
            ));
        }
        std::fs::rename(&tmp, &flac_path)?;
        Ok(())
    })();
    let path = match res {
        Ok(()) => {
            if let Err(e) = std::fs::remove_file(partial) {
                log::warn!("could not remove {}: {e}", partial.display());
            }
            flac_path
        }
        Err(e) => {
            log::error!("FLAC conversion failed, keeping WAV: {e}");
            let _ = std::fs::remove_file(&tmp);
            let wav_path = dir.join(format!("{stem}.wav"));
            if !wav_path.exists() && std::fs::rename(partial, &wav_path).is_ok() {
                wav_path
            } else {
                partial.to_path_buf()
            }
        }
    };
    let size = std::fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
    Ok(Finished {
        path,
        stem: stem.to_string(),
        size,
        duration: info.frames as f64 / info.rate.max(1) as f64,
        channels: info.channels,
        created_at,
    })
}

/// Stem of a partial file name, e.g. `2026-10-29_10-00-00` for `...partial.wav`.
pub fn partial_stem(path: &Path) -> Option<String> {
    let name = path.file_name()?.to_str()?;
    name.strip_suffix(PARTIAL_SUFFIX).map(str::to_string)
}

/// Finalise every orphaned `*.partial.wav` in `dir` (left by a crash), skipping `active`.
///
/// Not safe against a recording that starts meanwhile; the app uses [`Recorder::recover_dir`].
pub fn recover_dir(dir: &Path, active: Option<&Path>) -> Vec<Finished> {
    let Ok(rd) = std::fs::read_dir(dir) else {
        return Vec::new();
    };
    let mut out = Vec::new();
    for entry in rd.flatten() {
        let p = entry.path();
        if Some(p.as_path()) == active {
            continue;
        }
        let Some(stem) = partial_stem(&p) else {
            continue;
        };
        let created = entry
            .metadata()
            .and_then(|m| m.created().or_else(|_| m.modified()))
            .map(DateTime::<Utc>::from)
            .unwrap_or_else(|_| Utc::now());
        match finalize(&p, &stem, created) {
            Ok(f) => {
                log::info!("recovered {}", f.path.display());
                out.push(f);
            }
            Err(e) => log::error!("recovery of {} failed: {e}", p.display()),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finalize_converts_and_recovers_crashed_partial() {
        let dir = tempfile::tempdir().unwrap();
        let partial = dir.path().join("2026-01-01_00-00-00.partial.wav");
        let mut w = wav::WavWriter::create(&partial, 2, 16000).unwrap();
        w.write(&vec![100i16; 2 * 16000]).unwrap();
        w.checkpoint(false).unwrap();
        w.write(&vec![-5i16; 2 * 8000]).unwrap();
        drop(w); // crash: header still says 1 s
        let got = recover_dir(dir.path(), None);
        assert_eq!(got.len(), 1);
        let f = &got[0];
        assert_eq!(f.path, dir.path().join("2026-01-01_00-00-00.flac"));
        assert!((f.duration - 1.5).abs() < 1e-9);
        assert_eq!(f.channels, 2);
        assert!(!partial.exists());
        assert!(f.size > 0);
        // a second recording in the same second gets a distinct stem
        assert_eq!(
            unique_stem(dir.path(), "2026-01-01_00-00-00"),
            "2026-01-01_00-00-00_2"
        );
    }

    #[test]
    fn recover_skips_active_and_non_partials() {
        let dir = tempfile::tempdir().unwrap();
        let active = dir.path().join("a.partial.wav");
        wav::WavWriter::create(&active, 1, 16000)
            .unwrap()
            .finish()
            .unwrap();
        std::fs::write(dir.path().join("note.txt"), b"x").unwrap();
        assert!(recover_dir(dir.path(), Some(&active)).is_empty());
        assert!(active.exists());
    }

    fn opts() -> StartOptions {
        StartOptions {
            system: true,
            mic: true,
            ..Default::default()
        }
    }

    fn crashed_partial(path: &Path) {
        let mut w = wav::WavWriter::create(path, 2, 16000).unwrap();
        w.write(&vec![7i16; 2 * 4000]).unwrap();
        w.checkpoint(false).unwrap();
    }

    /// Recovery running while recordings start and stop over and over never touches a
    /// session's partial: every stop finalises its own complete FLAC, recovery only ever
    /// returns the real orphan.
    #[test]
    fn recovery_races_with_start_and_stop() {
        let dir = tempfile::tempdir().unwrap();
        let orphan = dir.path().join("2020-01-01_00-00-00.partial.wav");
        crashed_partial(&orphan);
        let env = testenv::FakeEnv::new();
        // the partial exists for a while before the session is installed
        *env.create_delay.lock().unwrap() = std::time::Duration::from_millis(15);
        let rec = Arc::new(Recorder::with_env(env.clone()));
        let done = Arc::new(AtomicBool::new(false));
        let recovered = {
            let (rec, done, d) = (rec.clone(), done.clone(), dir.path().to_path_buf());
            std::thread::spawn(move || {
                let mut all = Vec::new();
                while !done.load(Ordering::Acquire) {
                    all.extend(rec.recover_dir(&d).into_iter().filter(|r| !r.adopted));
                }
                all
            })
        };
        let mut stems = Vec::new();
        for _ in 0..12 {
            rec.start(&opts(), dir.path()).unwrap();
            assert!(rec.active_partial().unwrap().exists());
            std::thread::sleep(std::time::Duration::from_millis(60));
            let f = rec.stop().unwrap();
            assert_eq!(f.path.extension().unwrap(), "flac", "{f:?}");
            assert!(f.duration > 0.04, "{f:?}");
            stems.push(f.stem);
        }
        done.store(true, Ordering::Release);
        let recovered = recovered.join().unwrap();
        let names: Vec<_> = recovered.iter().map(|r| r.finished.stem.clone()).collect();
        assert_eq!(names, vec!["2020-01-01_00-00-00".to_string()]);
        assert!(!orphan.exists());
        for s in &stems {
            assert!(dir.path().join(format!("{s}.flac")).exists());
        }
    }

    /// `start` does not hold the lock while devices open: status and recovery stay responsive.
    /// Manual clock: the open blocks until the test moves the clock, so "responsive" is checked
    /// without real-time bounds (a call that waited for the open would never return).
    #[test]
    fn slow_device_open_does_not_block_status() {
        use std::sync::mpsc;
        let dir = tempfile::tempdir().unwrap();
        let env = testenv::FakeEnv::manual();
        *env.open_delay.lock().unwrap() = std::time::Duration::from_millis(400);
        let rec = Arc::new(Recorder::with_env(env.clone()));
        let h = {
            let (rec, d) = (rec.clone(), dir.path().to_path_buf());
            std::thread::spawn(move || rec.start(&opts(), &d).map(|_| ()))
        };
        let t = std::time::Instant::now();
        while env.opens(capture::SourceKind::System) == 0 {
            assert!(t.elapsed() < std::time::Duration::from_secs(10));
            std::thread::sleep(std::time::Duration::from_millis(1));
        }
        // the first open is now blocked on the clock
        let (tx, rx) = mpsc::channel();
        {
            let (rec, d) = (rec.clone(), dir.path().to_path_buf());
            std::thread::spawn(move || {
                let idle = rec.status(false).state == RecorderState::Idle;
                let recovered = rec.recover_dir(&d).is_empty();
                let refused = rec.start(&opts(), &d).is_err();
                let _ = tx.send((idle, recovered, refused));
            });
        }
        let (idle, recovered, refused) = rx
            .recv_timeout(std::time::Duration::from_secs(10))
            .expect("status / recovery / start blocked by the opening start");
        assert!(idle && recovered, "{idle} {recovered}");
        assert!(refused, "second start refused");
        // the devices open (one after the other)
        let t = std::time::Instant::now();
        while !h.is_finished() && t.elapsed() < std::time::Duration::from_secs(10) {
            env.advance(0.1);
            std::thread::sleep(std::time::Duration::from_millis(2));
        }
        h.join().unwrap().unwrap();
        assert_eq!(rec.state(), RecorderState::Recording);
        rec.stop().unwrap();
    }

    /// Finished files from an earlier run are reported as adopted (crash between finalising and
    /// queueing); files of this run and foreign names are not.
    #[test]
    fn stray_finished_recordings_are_adopted() {
        let dir = tempfile::tempdir().unwrap();
        let old = dir.path().join("2026-01-01_00-00-00.partial.wav");
        crashed_partial(&old);
        let old_flac = finalize(&old, "2026-01-01_00-00-00", Utc::now()).unwrap();
        std::fs::write(dir.path().join("2026-01-01_00-00-01_2.wav"), b"junk").unwrap();
        let mut w =
            wav::WavWriter::create(&dir.path().join("2026-01-01_00-00-02.wav"), 1, 16000).unwrap();
        w.write(&[1i16; 8000]).unwrap();
        w.finish().unwrap();
        std::fs::write(dir.path().join("song.flac"), b"x").unwrap();
        std::thread::sleep(std::time::Duration::from_millis(20));
        let rec = Recorder::with_env(testenv::FakeEnv::new());
        // a file finished by this run is not adopted
        let new = dir.path().join("2026-01-01_00-00-03.partial.wav");
        crashed_partial(&new);
        let new_flac = finalize(&new, "2026-01-01_00-00-03", Utc::now()).unwrap();
        let got = rec.recover_dir(dir.path());
        let paths: Vec<_> = got
            .iter()
            .map(|r| (r.finished.path.clone(), r.adopted))
            .collect();
        assert_eq!(
            paths,
            vec![
                (old_flac.path.clone(), true),
                (dir.path().join("2026-01-01_00-00-02.wav"), true),
            ],
            "{got:?}"
        );
        assert!((got[0].finished.duration - 0.25).abs() < 1e-9);
        assert_eq!(got[0].finished.channels, 2);
        assert!((got[1].finished.duration - 0.5).abs() < 1e-9);
        assert!(new_flac.path.exists());
        assert!(is_recording_stem("2026-01-01_00-00-00_12"));
        assert!(!is_recording_stem("2026-01-01_00-00-00.partial"));
        assert!(!is_recording_stem("2026-01-01_00-00-00_"));
    }

    /// The FLAC is shorter than the recording (e.g. a reader that stops at 4 GiB): the WAV is
    /// kept, nothing is deleted.
    #[test]
    fn finalize_keeps_wav_when_flac_is_short() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("2026-01-01_00-00-00.partial.wav");
        crashed_partial(&p);
        let f = finalize_with(&p, "2026-01-01_00-00-00", Utc::now(), |wav, flac| {
            flac::encode(wav, flac)?;
            Ok(flac::verify(flac, wav)? - 1)
        })
        .unwrap();
        assert_eq!(f.path, dir.path().join("2026-01-01_00-00-00.wav"));
        assert!(f.path.exists());
        assert!(!dir.path().join("2026-01-01_00-00-00.flac").exists());
        assert!(!dir.path().join("2026-01-01_00-00-00.flac.part").exists());
        assert_eq!(wav::WavReader::open(&f.path).unwrap().info.frames, 4000);
    }

    /// The recorder thread dies without a reason (a panic in a capture backend): `failure()`
    /// reports it, so the app's status loop stops the session, and `stop` finalises what was
    /// recorded before the panic.
    #[test]
    fn dead_recorder_thread_is_reported_and_stop_finalises() {
        let dir = tempfile::tempdir().unwrap();
        let env = testenv::FakeEnv::manual();
        *env.mic_panics_after.lock().unwrap() = Some(1.2);
        let rec = Recorder::with_env(env.clone());
        rec.start(&opts(), dir.path()).unwrap();
        // to 1.1 s (before the panic), then until the recorder has processed it
        for _ in 0..110 {
            env.advance(0.01);
            std::thread::sleep(std::time::Duration::from_millis(1));
        }
        let t = std::time::Instant::now();
        while rec.status(false).elapsed < 1.1 && t.elapsed() < std::time::Duration::from_secs(10) {
            std::thread::sleep(std::time::Duration::from_millis(2));
        }
        assert_eq!(rec.failure(), None);
        env.advance(0.2); // past the panic
        let t = std::time::Instant::now();
        while rec.failure().is_none() && t.elapsed() < std::time::Duration::from_secs(5) {
            std::thread::sleep(std::time::Duration::from_millis(20));
        }
        assert_eq!(rec.failure().as_deref(), Some(THREAD_ENDED));
        assert_eq!(rec.state(), RecorderState::Recording);
        let f = rec.stop().unwrap();
        assert_eq!(f.path.extension().unwrap(), "flac");
        assert!((0.8..1.2).contains(&f.duration), "{f:?}");
        assert!(!dir
            .path()
            .join(format!("{}{PARTIAL_SUFFIX}", f.stem))
            .exists());
        assert_eq!(rec.state(), RecorderState::Idle);
        assert_eq!(rec.failure(), None);
    }

    /// Crash after the FLAC was renamed into place but before the partial was removed: the
    /// next finalise (recovery) keeps the verified FLAC and removes the partial, so only one
    /// file is left to queue. A FLAC that does not match keeps both.
    #[test]
    fn finalize_after_a_crash_past_the_rename_keeps_one_file() {
        let dir = tempfile::tempdir().unwrap();
        let stem = "2026-01-01_00-00-00";
        let p = dir.path().join(format!("{stem}{PARTIAL_SUFFIX}"));
        crashed_partial(&p);
        let flac_path = dir.path().join(format!("{stem}.flac"));
        wav::repair(&p).unwrap();
        flac::encode(&p, &flac_path).unwrap();
        let f = finalize(&p, stem, Utc::now()).unwrap();
        assert_eq!(f.path, flac_path);
        assert!(!p.exists());
        assert!(!dir.path().join(format!("{stem}.wav")).exists());
        // no leftover files, nothing adopted twice
        let names: Vec<_> = std::fs::read_dir(dir.path())
            .unwrap()
            .map(|e| e.unwrap().file_name().into_string().unwrap())
            .collect();
        assert_eq!(names, vec![format!("{stem}.flac")]);

        // a different recording under the same name: both are kept
        let stem = "2026-01-01_00-00-01";
        let other = dir.path().join("other.wav");
        crashed_partial(&other);
        let flac_path = dir.path().join(format!("{stem}.flac"));
        wav::repair(&other).unwrap();
        flac::encode(&other, &flac_path).unwrap();
        let p = dir.path().join(format!("{stem}{PARTIAL_SUFFIX}"));
        let mut w = wav::WavWriter::create(&p, 2, 16000).unwrap();
        w.write(&vec![9i16; 2 * 4000]).unwrap();
        w.finish().unwrap();
        let f = finalize(&p, stem, Utc::now()).unwrap();
        assert_eq!(f.path, dir.path().join(format!("{stem}.wav")));
        assert!(flac_path.exists() && f.path.exists());
    }

    /// The disk fails for good: `failure()` reports it, the state stays Recording until `stop`,
    /// which still finalises what reached the disk.
    #[test]
    fn failure_is_reported_and_stop_finalises() {
        let dir = tempfile::tempdir().unwrap();
        let env = testenv::FakeEnv::new();
        let mut rec = Recorder::with_env(env.clone());
        rec.max_backlog_s = 0.3;
        rec.start(&opts(), dir.path()).unwrap();
        std::thread::sleep(std::time::Duration::from_millis(1300)); // first checkpoint at 1 s
        env.fail_writes.store(true, Ordering::SeqCst);
        let t = std::time::Instant::now();
        while rec.failure().is_none() && t.elapsed() < std::time::Duration::from_secs(5) {
            std::thread::sleep(std::time::Duration::from_millis(20));
        }
        let msg = rec.failure().expect("session failed");
        assert_eq!(rec.state(), RecorderState::Recording);
        assert!(rec.status(false).warnings.contains(&msg));
        let f = rec.stop().unwrap();
        assert_eq!(f.path.extension().unwrap(), "flac");
        assert!((0.9..1.4).contains(&f.duration), "{f:?}");
        assert_eq!(rec.state(), RecorderState::Idle);
        assert_eq!(rec.failure(), None);
    }
}
