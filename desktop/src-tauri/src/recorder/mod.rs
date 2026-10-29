//! Recorder: capture -> timeline -> `<stem>.partial.wav` -> verified `<stem>.flac`.

pub mod capture;
pub mod engine;
pub mod flac;
pub mod resample;
pub mod timeline;
#[cfg(windows)]
mod wasapi_capture;
pub mod wav;

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;

use chrono::{DateTime, Local, Utc};

use crate::ipc::{Levels, RecorderState, RecorderStatus, StartOptions};
use engine::{Live, SessionConfig, SessionResult};

pub const PARTIAL_SUFFIX: &str = ".partial.wav";

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
    last_file: Option<String>,
    /// Peak levels already reported by `status(true)` are cleared here.
    last_status: Option<RecorderStatus>,
}

pub struct Recorder {
    inner: Mutex<Inner>,
}

impl Default for Recorder {
    fn default() -> Self {
        Self::new()
    }
}

/// Pick `<dir>/<stem>` with a stem that clashes with no existing recording file.
pub fn unique_stem(dir: &Path, base: &str) -> String {
    let taken = |s: &str| {
        [".flac", PARTIAL_SUFFIX, ".wav", ".flac.part"]
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

impl Recorder {
    pub fn new() -> Self {
        Self {
            inner: Mutex::new(Inner {
                state: RecorderState::Idle,
                session: None,
                last_file: None,
                last_status: None,
            }),
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

    pub fn start(&self, opts: &StartOptions, out_dir: &Path) -> Result<RecorderStatus, String> {
        let mut inner = self.lock();
        if inner.state != RecorderState::Idle {
            return Err("already recording".into());
        }
        if !opts.system && !opts.mic {
            return Err("enable system audio and/or microphone".into());
        }
        std::fs::create_dir_all(out_dir)
            .map_err(|e| format!("cannot create {}: {e}", out_dir.display()))?;
        let now = Local::now();
        let stem = unique_stem(out_dir, &crate::util::recording_stem(now));
        let partial = out_dir.join(format!("{stem}{PARTIAL_SUFFIX}"));
        let live = Arc::new(Mutex::new(Live::default()));
        let stop = Arc::new(AtomicBool::new(false));
        let thread = engine::spawn(
            SessionConfig {
                system: opts.system,
                mic: opts.mic,
                output_device: opts.output_device.clone(),
                input_device: opts.input_device.clone(),
                partial: partial.clone(),
            },
            live.clone(),
            stop.clone(),
        )?;
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
            inner.state = RecorderState::Finalizing;
            inner.session.take().expect("recording without a session")
        };
        session.stop.store(true, Ordering::Release);
        let result = session
            .thread
            .join()
            .map_err(|_| "recorder thread panicked".to_string())
            .and_then(|r| r);
        if let Err(e) = &result {
            log::error!("recorder thread ended with: {e}");
        }
        // Whatever happened, the partial file (if any) is finalised, never discarded.
        let finished = finalize(&session.partial, &session.stem, session.created_at);
        let mut inner = self.lock();
        inner.state = RecorderState::Idle;
        match &finished {
            Ok(f) => inner.last_file = Some(f.path.to_string_lossy().into_owned()),
            Err(e) => log::error!("finalise failed: {e}"),
        }
        finished
    }
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
    let dir = partial.parent().unwrap_or(Path::new("."));
    let info = wav::repair(partial).map_err(|e| format!("{}: {e}", partial.display()))?;
    let flac_path = dir.join(format!("{stem}.flac"));
    let tmp = dir.join(format!("{stem}.flac.part"));
    let res = (|| -> std::io::Result<()> {
        if flac_path.exists() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::AlreadyExists,
                format!("{} exists", flac_path.display()),
            ));
        }
        flac::encode(partial, &tmp)?;
        flac::verify(&tmp, partial)?;
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
}
