//! Upload queue worker: chunked upload -> `POST /api/jobs` -> poll the job. Survives restarts
//! (state in `recordings.json`), retries transient failures with exponential backoff, and never
//! deletes a recording.

pub mod backoff;
pub mod client;
pub mod queue;

use std::path::Path;
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

use chrono::{DateTime, Utc};

use crate::config::ConfigStore;
use crate::ipc::{Recording, UploadInfo, UploadState};
use crate::recorder::Finished;
use crate::util::iso;
use client::{ApiError, Client};
use queue::{Entry, Inserted, Stage, Store, UploadSession};

pub type OnChange = Arc<dyn Fn(&Recording) + Send + Sync>;

pub struct Uploader {
    store: Arc<Store>,
    config: Arc<ConfigStore>,
    wake: (Mutex<bool>, Condvar),
    on_change: OnChange,
}

fn chrono_after(d: Duration) -> DateTime<Utc> {
    Utc::now() + chrono::Duration::from_std(d).unwrap_or(chrono::Duration::zero())
}

fn jitter() -> f64 {
    rand::random::<f64>() * 2.0 - 1.0
}

/// Poll interval for a running job, by job age.
fn poll_interval(age: chrono::Duration) -> Duration {
    match age.num_seconds() {
        s if s < 120 => Duration::from_secs(5),
        s if s < 1800 => Duration::from_secs(15),
        _ => Duration::from_secs(60),
    }
}

fn mtime_ms(m: &std::fs::Metadata) -> Option<i64> {
    let t = m.modified().ok()?;
    Some(DateTime::<Utc>::from(t).timestamp_millis())
}

/// Error text for a 404 from `POST /api/jobs` while the upload exists.
fn job_404_message(detail: &str) -> String {
    if detail.contains("term set") {
        format!(
            "术语表不存在，请在设置中修正术语表后重试 / a term set of this job no longer exists; \
             fix the term sets in the settings and retry ({detail})"
        )
    } else {
        format!("创建转写任务失败 / job creation failed ({detail})")
    }
}

pub fn recording_from(f: &Finished) -> Recording {
    Recording {
        id: f.stem.clone(),
        path: f.path.to_string_lossy().into_owned(),
        name: f
            .path
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default(),
        size: f.size,
        duration: (f.duration * 1000.0).round() / 1000.0,
        created_at: iso(f.created_at),
        upload: UploadInfo {
            state: UploadState::Pending,
            attempts: 0,
            upload_id: None,
            job_id: None,
            transcript_id: None,
            error: None,
            next_retry_at: None,
        },
    }
}

impl Uploader {
    pub fn new(store: Arc<Store>, config: Arc<ConfigStore>, on_change: OnChange) -> Arc<Self> {
        Arc::new(Self {
            store,
            config,
            wake: (Mutex::new(false), Condvar::new()),
            on_change,
        })
    }

    pub fn store(&self) -> &Store {
        &self.store
    }

    /// Start the background worker thread (entries added later are picked up via `wake`).
    pub fn start(self: &Arc<Self>) {
        let me = self.clone();
        if let Err(e) = std::thread::Builder::new()
            .name("uploader".into())
            .spawn(move || me.run())
        {
            log::error!("cannot start the upload worker: {e}");
        }
    }

    pub fn wake(&self) {
        *crate::util::lock(&self.wake.0) = true;
        self.wake.1.notify_all();
    }

    fn emit(&self, e: &Entry) {
        (self.on_change)(&e.recording);
    }

    fn update(&self, id: &str, f: impl FnOnce(&mut Entry)) -> Option<Entry> {
        let before = self.store.get(id)?;
        let after = self.store.update(id, f)?;
        if after.recording != before.recording {
            self.emit(&after);
        }
        Some(after)
    }

    /// Register a finished recording; queue it when `auto`. A file that is already indexed
    /// keeps its entry (state, job, transcript) untouched; see `Store::insert`.
    pub fn add_finished(&self, f: &Finished, auto: bool) -> Recording {
        let entry = Entry::new(recording_from(f), f.channels, auto);
        match self.store.insert(entry) {
            Inserted::New(e) => {
                self.emit(&e);
                if auto {
                    self.wake();
                }
                e.recording
            }
            Inserted::Existing(e) => e.recording,
        }
    }

    /// Whether this file is already in the index (for recovery's adopted files).
    pub fn has_path(&self, path: &Path) -> bool {
        self.store.contains_path(&path.to_string_lossy())
    }

    /// `recording_upload`: queue now (manual upload or retry).
    pub fn enqueue(&self, id: &str) -> Result<Recording, String> {
        let wants_job = self.config.get_with_key().job_options.is_some();
        let e = self
            .update(id, |e| {
                e.queued = true;
                let u = &mut e.recording.upload;
                match u.state {
                    UploadState::Done => {}
                    UploadState::Uploaded => {
                        if wants_job {
                            u.state = UploadState::Pending;
                        }
                    }
                    UploadState::Failed => {
                        if e.failed_stage == Some(Stage::Transcription) {
                            u.job_id = None;
                            u.transcript_id = None;
                        }
                        u.state = if u.job_id.is_some() {
                            UploadState::Transcribing
                        } else {
                            UploadState::Pending
                        };
                        u.next_retry_at = None;
                        e.failures = 0;
                        e.failed_stage = None;
                        e.next_poll_at = None;
                    }
                    UploadState::Pending | UploadState::Uploading | UploadState::Transcribing => {
                        u.next_retry_at = None;
                        e.next_poll_at = None;
                    }
                }
            })
            .ok_or_else(|| format!("unknown recording {id}"))?;
        self.wake();
        Ok(e.recording)
    }

    /// Backend config changed: retry failed uploads / job creation now (the failure was likely
    /// a wrong URL, key or target dir), whether or not a backoff retry was scheduled.
    pub fn config_changed(&self) {
        let now = iso(Utc::now());
        // One pass, one save (none when nothing is re-armed).
        let changed = self.store.update_all(|e| {
            let u = &mut e.recording.upload;
            if e.queued
                && u.state == UploadState::Failed
                && matches!(e.failed_stage, Some(Stage::Upload | Stage::Job))
            {
                u.next_retry_at = Some(now.clone());
            }
        });
        for e in &changed {
            self.emit(e);
        }
        self.wake();
    }

    fn run(&self) {
        loop {
            let wait = match self.store.next_due() {
                Some((id, due)) if due <= Utc::now() => {
                    self.step(&id);
                    continue;
                }
                Some((_, due)) => (due - Utc::now())
                    .to_std()
                    .unwrap_or(Duration::ZERO)
                    .min(Duration::from_secs(60)),
                None => Duration::from_secs(60),
            };
            let mut flag = crate::util::lock(&self.wake.0);
            if !*flag {
                flag = match self.wake.1.wait_timeout(flag, wait) {
                    Ok((g, _)) => g,
                    Err(p) => p.into_inner().0,
                };
            }
            *flag = false;
        }
    }

    /// One unit of work on one entry.
    pub fn step(&self, id: &str) {
        let Some(e) = self.store.get(id) else { return };
        let cfg = self.config.get_with_key();
        let client = match Client::new(&cfg) {
            Ok(c) => c,
            Err(err) => {
                self.fail(id, Stage::Upload, ApiError::Transient(err));
                return;
            }
        };
        let u = e.upload();
        // The file is only needed until it is on the server: creating the job and polling work
        // without it (if the backend lost the upload, the re-upload lands here again).
        if u.job_id.is_none() && u.upload_id.is_none() && !Path::new(&e.recording.path).is_file() {
            self.fail(
                id,
                Stage::Upload,
                ApiError::Permanent("录音文件不存在 / recording file is missing".into()),
            );
            return;
        }
        if u.job_id.is_some() {
            self.poll(&client, &e);
        } else if u.upload_id.is_some() {
            self.create_job(&client, &e, &cfg);
        } else if self.upload(&client, &e, &cfg.target_dir) {
            if let Some(e) = self.store.get(id) {
                self.create_job(&client, &e, &cfg);
            }
        }
    }

    fn fail(&self, id: &str, stage: Stage, err: ApiError) {
        log::warn!("{id}: {stage:?} failed: {}", err.message());
        self.update(id, |e| {
            let u = &mut e.recording.upload;
            u.state = UploadState::Failed;
            u.error = Some(err.message().to_string());
            e.failed_stage = Some(stage);
            match err {
                ApiError::Transient(_) | ApiError::NotFound(_) => {
                    e.failures += 1;
                    let d = backoff::delay(e.failures, jitter());
                    u.next_retry_at = Some(iso(chrono_after(d)));
                }
                ApiError::Permanent(_) => u.next_retry_at = None,
            }
        });
    }

    /// Returns true when the file is on the server.
    fn upload(&self, client: &Client, e: &Entry, target_dir: &str) -> bool {
        let id = e.recording.id.as_str();
        let path = Path::new(&e.recording.path);
        let (size, mtime_ms) = match std::fs::metadata(path) {
            Ok(m) => (m.len(), mtime_ms(&m)),
            Err(err) => {
                self.fail(id, Stage::Upload, ApiError::Permanent(err.to_string()));
                return false;
            }
        };
        self.update(id, |e| {
            let u = &mut e.recording.upload;
            u.state = UploadState::Uploading;
            u.attempts += 1;
            u.next_retry_at = None;
        });
        // Resume only if the file is unchanged since the session started.
        let unchanged =
            |s: &UploadSession| s.size == size && mtime_ms.is_some() && s.mtime_ms == mtime_ms;
        let mut session = match e.session.clone().filter(unchanged) {
            Some(s) => s,
            None => match client.init_upload(&e.recording.name, size, target_dir) {
                Ok(o) => {
                    let s = UploadSession {
                        session_id: o.session_id,
                        part_size: o.part_size,
                        total_parts: o.total_parts,
                        next_part: 0,
                        size,
                        mtime_ms,
                    };
                    self.store.update(id, |e| e.session = Some(s.clone()));
                    s
                }
                Err(err) => {
                    self.fail(id, Stage::Upload, err);
                    return false;
                }
            },
        };
        while session.next_part < session.total_parts {
            let n = session.next_part;
            if let Err(err) = client.put_part(&session.session_id, n, path, session.part_size, size)
            {
                if matches!(err, ApiError::NotFound(_)) {
                    // Session expired on the server: start over next time.
                    self.store.update(id, |e| e.session = None);
                }
                self.fail(id, Stage::Upload, err);
                return false;
            }
            session.next_part += 1;
            let s = session.clone();
            self.store.update(id, |e| e.session = Some(s));
        }
        match client.complete(&session.session_id) {
            Ok(upload_id) => {
                log::info!("{id}: uploaded as {upload_id}");
                self.update(id, |e| {
                    e.session = None;
                    e.failures = 0;
                    e.failed_stage = None;
                    let u = &mut e.recording.upload;
                    u.upload_id = Some(upload_id);
                    u.state = UploadState::Uploaded;
                    u.error = None;
                });
                true
            }
            Err(err) => {
                // Transient (incl. 409, a concurrent duplicate): keep the session; the backend's
                // complete is idempotent, so the retry returns the upload. Session gone or
                // rejected: the next attempt starts a fresh upload instead of replaying a
                // failing complete forever.
                if matches!(err, ApiError::NotFound(_) | ApiError::Permanent(_)) {
                    self.store.update(id, |e| e.session = None);
                }
                self.fail(id, Stage::Upload, err);
                false
            }
        }
    }

    fn create_job(&self, client: &Client, e: &Entry, cfg: &crate::ipc::BackendConfig) {
        let id = e.recording.id.as_str();
        let Some(upload_id) = e.upload().upload_id.clone() else {
            return;
        };
        let Some(body) = client::job_body(cfg, &upload_id, e.channels) else {
            // Upload only: final state.
            self.update(id, |e| {
                e.queued = false;
                e.recording.upload.state = UploadState::Uploaded;
                e.recording.upload.next_retry_at = None;
            });
            return;
        };
        match client.create_job(&body) {
            Ok(job) => {
                log::info!("{id}: job {} created", job.id);
                self.update(id, |e| {
                    e.failures = 0;
                    e.failed_stage = None;
                    e.job_started_at = Some(Utc::now());
                    e.next_poll_at = Some(chrono_after(Duration::from_secs(3)));
                    let u = &mut e.recording.upload;
                    u.job_id = Some(job.id);
                    u.state = UploadState::Transcribing;
                    u.error = None;
                    u.next_retry_at = None;
                });
            }
            Err(ApiError::NotFound(msg)) => {
                // 404 means "upload not found" or "term set '<id>' not found": ask the backend
                // which one before uploading the whole file again.
                match client.get_upload(&upload_id) {
                    Err(ApiError::NotFound(_)) => {
                        log::warn!("{id}: upload {upload_id} is gone on the backend; re-uploading");
                        self.store
                            .update(id, |e| e.recording.upload.upload_id = None);
                        self.fail(id, Stage::Upload, ApiError::NotFound(msg));
                    }
                    Ok(()) => self.fail(id, Stage::Job, ApiError::Permanent(job_404_message(&msg))),
                    Err(err) => {
                        self.fail(id, Stage::Job, ApiError::Transient(err.message().into()))
                    }
                }
            }
            Err(err) => self.fail(id, Stage::Job, err),
        }
    }

    fn poll(&self, client: &Client, e: &Entry) {
        let id = e.recording.id.as_str();
        let job_id = e.upload().job_id.clone().unwrap_or_default();
        match client.get_job(&job_id) {
            Ok(job) => match job.status.as_str() {
                "succeeded" => {
                    log::info!("{id}: job {job_id} succeeded");
                    self.update(id, |e| {
                        e.queued = false;
                        e.failures = 0;
                        e.failed_stage = None;
                        e.next_poll_at = None;
                        let u = &mut e.recording.upload;
                        u.state = UploadState::Done;
                        u.transcript_id = job.transcript_id.clone();
                        u.error = None;
                        u.next_retry_at = None;
                    });
                }
                "failed" | "cancelled" => {
                    let msg = job
                        .error
                        .clone()
                        .map(|m| client::redact(&m))
                        .unwrap_or_else(|| format!("job {}", job.status));
                    self.update(id, |e| {
                        e.failed_stage = Some(Stage::Transcription);
                        e.next_poll_at = None;
                        let u = &mut e.recording.upload;
                        u.state = UploadState::Failed;
                        u.error = Some(msg);
                        u.next_retry_at = None;
                    });
                }
                _ => {
                    let age = Utc::now() - e.job_started_at.unwrap_or_else(Utc::now);
                    self.update(id, |e| {
                        e.failures = 0;
                        e.next_poll_at = Some(chrono_after(poll_interval(age)));
                        let u = &mut e.recording.upload;
                        u.state = UploadState::Transcribing;
                        u.error = None;
                        u.next_retry_at = None;
                    });
                }
            },
            Err(ApiError::Transient(msg)) => {
                // Backend unreachable while transcribing: keep polling with backoff.
                self.update(id, |e| {
                    e.failures += 1;
                    let d = backoff::delay(e.failures, jitter());
                    e.next_poll_at = Some(chrono_after(d));
                    e.recording.upload.error = Some(msg);
                });
            }
            Err(ApiError::NotFound(msg)) => self.update_failed(id, Stage::Transcription, msg),
            Err(ApiError::Permanent(msg)) => self.update_failed(id, Stage::Job, msg),
        }
    }

    fn update_failed(&self, id: &str, stage: Stage, msg: String) {
        self.update(id, |e| {
            e.failed_stage = Some(stage);
            let u = &mut e.recording.upload;
            u.state = UploadState::Failed;
            u.error = Some(msg);
            u.next_retry_at = None;
        });
    }
}

#[cfg(test)]
mod tests;
