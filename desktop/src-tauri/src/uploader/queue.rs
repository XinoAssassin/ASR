//! Persistent recordings index + upload state (`recordings.json` in the app data dir).

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

use crate::ipc::{Recording, UploadInfo, UploadState};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Stage {
    Upload,
    Job,
    Transcription,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct UploadSession {
    pub session_id: String,
    pub part_size: u64,
    pub total_parts: u64,
    pub next_part: u64,
    pub size: u64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Entry {
    pub recording: Recording,
    pub channels: u16,
    /// Eligible for automatic processing by the worker.
    pub queued: bool,
    #[serde(default)]
    pub session: Option<UploadSession>,
    #[serde(default)]
    pub failed_stage: Option<Stage>,
    /// Consecutive failures of the current stage (drives the backoff).
    #[serde(default)]
    pub failures: u32,
    #[serde(default)]
    pub next_poll_at: Option<DateTime<Utc>>,
    #[serde(default)]
    pub job_started_at: Option<DateTime<Utc>>,
}

impl Entry {
    pub fn new(recording: Recording, channels: u16, queued: bool) -> Self {
        Self {
            recording,
            channels,
            queued,
            session: None,
            failed_stage: None,
            failures: 0,
            next_poll_at: None,
            job_started_at: None,
        }
    }

    pub fn upload(&self) -> &UploadInfo {
        &self.recording.upload
    }

    /// When the worker should look at this entry next (None = not scheduled).
    pub fn due_at(&self) -> Option<DateTime<Utc>> {
        if !self.queued {
            return None;
        }
        let parse = |s: &Option<String>| {
            s.as_deref()
                .and_then(|v| DateTime::parse_from_rfc3339(v).ok())
                .map(|d| d.with_timezone(&Utc))
        };
        let u = self.upload();
        match u.state {
            UploadState::Pending | UploadState::Uploading => {
                Some(parse(&u.next_retry_at).unwrap_or(DateTime::<Utc>::MIN_UTC))
            }
            UploadState::Transcribing => {
                Some(self.next_poll_at.unwrap_or(DateTime::<Utc>::MIN_UTC))
            }
            UploadState::Failed => parse(&u.next_retry_at),
            UploadState::Uploaded | UploadState::Done => None,
        }
    }
}

pub struct Store {
    path: PathBuf,
    entries: Mutex<Vec<Entry>>,
}

impl Store {
    pub fn open(path: &Path) -> Self {
        let entries: Vec<Entry> = std::fs::read(path)
            .ok()
            .and_then(|b| match serde_json::from_slice(&b) {
                Ok(v) => Some(v),
                Err(e) => {
                    log::error!("{} unreadable ({e}); keeping a copy", path.display());
                    let _ = std::fs::copy(path, path.with_extension("json.bad"));
                    None
                }
            })
            .unwrap_or_default();
        Self {
            path: path.to_path_buf(),
            entries: Mutex::new(entries),
        }
    }

    fn save(&self, entries: &[Entry]) {
        match serde_json::to_vec_pretty(entries) {
            Ok(b) => {
                if let Err(e) = crate::util::write_atomic(&self.path, &b) {
                    log::error!("cannot write {}: {e}", self.path.display());
                }
            }
            Err(e) => log::error!("cannot serialise the queue: {e}"),
        }
    }

    /// Recordings, newest first.
    pub fn list(&self) -> Vec<Recording> {
        let mut v: Vec<Recording> = self
            .entries
            .lock()
            .unwrap()
            .iter()
            .map(|e| e.recording.clone())
            .collect();
        v.sort_by(|a, b| b.created_at.cmp(&a.created_at));
        v
    }

    pub fn get(&self, id: &str) -> Option<Entry> {
        self.entries
            .lock()
            .unwrap()
            .iter()
            .find(|e| e.recording.id == id)
            .cloned()
    }

    pub fn insert(&self, entry: Entry) {
        let mut v = self.entries.lock().unwrap();
        v.retain(|e| e.recording.id != entry.recording.id);
        v.push(entry);
        self.save(&v);
    }

    /// Mutate one entry and persist. Returns the updated entry.
    pub fn update(&self, id: &str, f: impl FnOnce(&mut Entry)) -> Option<Entry> {
        let mut v = self.entries.lock().unwrap();
        let e = v.iter_mut().find(|e| e.recording.id == id)?;
        f(e);
        let out = e.clone();
        self.save(&v);
        Some(out)
    }

    /// The entry due earliest, with its due time.
    pub fn next_due(&self) -> Option<(String, DateTime<Utc>)> {
        self.entries
            .lock()
            .unwrap()
            .iter()
            .filter_map(|e| e.due_at().map(|d| (e.recording.id.clone(), d)))
            .min_by_key(|(_, d)| *d)
    }

    pub fn ids(&self) -> Vec<String> {
        self.entries
            .lock()
            .unwrap()
            .iter()
            .map(|e| e.recording.id.clone())
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    pub fn rec(id: &str, created: &str) -> Recording {
        Recording {
            id: id.into(),
            path: format!("/r/{id}.flac"),
            name: format!("{id}.flac"),
            size: 10,
            duration: 1.0,
            created_at: created.into(),
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

    #[test]
    fn persists_and_orders() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("recordings.json");
        let s = Store::open(&p);
        s.insert(Entry::new(rec("a", "2026-01-01T00:00:00Z"), 2, true));
        s.insert(Entry::new(rec("b", "2026-01-02T00:00:00Z"), 1, false));
        s.update("a", |e| {
            e.recording.upload.state = UploadState::Uploading;
            e.session = Some(UploadSession {
                session_id: "s".into(),
                part_size: 8,
                total_parts: 2,
                next_part: 1,
                size: 10,
            });
        });
        let s2 = Store::open(&p);
        let l = s2.list();
        assert_eq!(l[0].id, "b");
        assert_eq!(l[1].upload.state, UploadState::Uploading);
        assert_eq!(s2.get("a").unwrap().session.unwrap().next_part, 1);
        // only the queued one is due; an interrupted upload resumes immediately
        let (id, _) = s2.next_due().unwrap();
        assert_eq!(id, "a");
    }

    #[test]
    fn due_rules() {
        let mut e = Entry::new(rec("a", "x"), 2, true);
        e.recording.upload.state = UploadState::Failed;
        assert_eq!(e.due_at(), None); // permanent failure: manual retry only
        e.recording.upload.next_retry_at = Some("2030-01-01T00:00:00Z".into());
        assert_eq!(
            e.due_at().unwrap().to_rfc3339(),
            "2030-01-01T00:00:00+00:00"
        );
        e.recording.upload.state = UploadState::Done;
        assert_eq!(e.due_at(), None);
        e.recording.upload.state = UploadState::Pending;
        e.queued = false;
        assert_eq!(e.due_at(), None);
    }

    #[test]
    fn corrupt_index_is_kept_aside() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("recordings.json");
        std::fs::write(&p, b"{not json").unwrap();
        let s = Store::open(&p);
        assert!(s.list().is_empty());
        assert!(dir.path().join("recordings.json.bad").exists());
    }
}
