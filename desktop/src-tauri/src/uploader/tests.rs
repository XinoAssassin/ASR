//! Uploader tests against an in-process mock of the backend's upload/job API.

use super::*;
use crate::ipc::BackendConfig;
use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::sync::atomic::{AtomicUsize, Ordering};

/// (filename, size, parts)
type Session = (String, u64, HashMap<u64, Vec<u8>>);

#[derive(Default)]
struct Mock {
    part_size: u64,
    /// session -> (filename, size, parts)
    sessions: HashMap<String, Session>,
    uploads: HashMap<String, Vec<u8>>,
    jobs: Vec<serde_json::Value>,
    polls: usize,
    part_puts: Vec<u64>,
    keys: Vec<Option<String>>,
    /// Status codes to return for the next requests (front first).
    inject: Vec<u16>,
    /// (request number, status): fail that request (0-based count of all requests).
    inject_at: Vec<(usize, u16)>,
    requests: usize,
}

struct Server {
    url: String,
    state: Arc<Mutex<Mock>>,
    hits: Arc<AtomicUsize>,
}

fn respond(stream: &mut std::net::TcpStream, code: u16, body: &str) {
    let _ = write!(
        stream,
        "HTTP/1.1 {code} X\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
}

fn serve(part_size: u64) -> Server {
    let l = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", l.local_addr().unwrap());
    let state = Arc::new(Mutex::new(Mock {
        part_size,
        ..Default::default()
    }));
    let hits = Arc::new(AtomicUsize::new(0));
    let (st, h) = (state.clone(), hits.clone());
    std::thread::spawn(move || {
        for s in l.incoming() {
            let Ok(mut s) = s else { continue };
            h.fetch_add(1, Ordering::SeqCst);
            let mut r = BufReader::new(s.try_clone().unwrap());
            let mut line = String::new();
            r.read_line(&mut line).unwrap();
            let mut parts = line.split_whitespace();
            let method = parts.next().unwrap_or("").to_string();
            let path = parts.next().unwrap_or("").to_string();
            let mut len = 0usize;
            let mut key = None;
            loop {
                let mut hl = String::new();
                r.read_line(&mut hl).unwrap();
                let hl = hl.trim_end();
                if hl.is_empty() {
                    break;
                }
                let (k, v) = hl.split_once(':').unwrap();
                match k.to_ascii_lowercase().as_str() {
                    "content-length" => len = v.trim().parse().unwrap(),
                    "x-api-key" => key = Some(v.trim().to_string()),
                    _ => {}
                }
            }
            let mut body = vec![0u8; len];
            r.read_exact(&mut body).unwrap();
            let mut m = st.lock().unwrap();
            m.keys.push(key);
            let nreq = m.requests;
            m.requests += 1;
            if let Some(&(_, code)) = m.inject_at.iter().find(|(n, _)| *n == nreq) {
                respond(&mut s, code, r#"{"detail":"injected"}"#);
                continue;
            }
            if !m.inject.is_empty() {
                let code = m.inject.remove(0);
                respond(&mut s, code, r#"{"detail":"injected"}"#);
                continue;
            }
            let seg: Vec<&str> = path.trim_start_matches('/').split('/').collect();
            match (method.as_str(), seg.as_slice()) {
                ("POST", ["api", "uploads", "init"]) => {
                    let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
                    let size = v["size"].as_u64().unwrap();
                    let sid = format!("s{}", m.sessions.len());
                    let ps = m.part_size;
                    let total = size.div_ceil(ps).max(1);
                    m.sessions.insert(
                        sid.clone(),
                        (v["filename"].as_str().unwrap().into(), size, HashMap::new()),
                    );
                    respond(
                        &mut s,
                        201,
                        &format!(
                            r#"{{"session_id":"{sid}","part_size":{ps},"total_parts":{total}}}"#
                        ),
                    );
                }
                ("PUT", ["api", "uploads", sid, "parts", n]) => {
                    let n: u64 = n.parse().unwrap();
                    m.part_puts.push(n);
                    match m.sessions.get_mut(*sid) {
                        Some(sess) => {
                            sess.2.insert(n, body);
                            respond(&mut s, 200, &format!(r#"{{"n":{n},"size":{len}}}"#));
                        }
                        None => respond(&mut s, 404, r#"{"detail":"unknown session"}"#),
                    }
                }
                ("POST", ["api", "uploads", sid, "complete"]) => {
                    let Some((_, size, parts)) = m.sessions.remove(*sid) else {
                        respond(&mut s, 404, r#"{"detail":"unknown session"}"#);
                        continue;
                    };
                    let mut keys: Vec<_> = parts.keys().copied().collect();
                    keys.sort();
                    let data: Vec<u8> = keys.iter().flat_map(|k| parts[k].clone()).collect();
                    assert_eq!(data.len() as u64, size);
                    let uid = format!("u{}", m.uploads.len());
                    m.uploads.insert(uid.clone(), data);
                    respond(&mut s, 201, &format!(r#"{{"id":"{uid}"}}"#));
                }
                ("POST", ["api", "jobs"]) => {
                    let v: serde_json::Value = serde_json::from_slice(&body).unwrap();
                    if !m.uploads.contains_key(v["upload_id"].as_str().unwrap()) {
                        respond(&mut s, 404, r#"{"detail":"upload not found"}"#);
                        continue;
                    }
                    m.jobs.push(v);
                    let id = format!("j{}", m.jobs.len());
                    respond(
                        &mut s,
                        201,
                        &format!(r#"{{"id":"{id}","status":"queued"}}"#),
                    );
                }
                ("GET", ["api", "jobs", id]) => {
                    m.polls += 1;
                    let body = if m.polls >= 2 {
                        format!(r#"{{"id":"{id}","status":"succeeded","transcript_id":"t-{id}"}}"#)
                    } else {
                        format!(r#"{{"id":"{id}","status":"running"}}"#)
                    };
                    respond(&mut s, 200, &body);
                }
                _ => respond(&mut s, 404, r#"{"detail":"no route"}"#),
            }
        }
    });
    Server { url, state, hits }
}

struct Env {
    _dir: tempfile::TempDir,
    up: Arc<Uploader>,
    events: Arc<Mutex<Vec<Recording>>>,
    file: Vec<u8>,
    data_dir: std::path::PathBuf,
}

fn env(url: &str, key: Option<&str>, opts: Option<serde_json::Value>) -> Env {
    let dir = tempfile::tempdir().unwrap();
    let data_dir = dir.path().join("data");
    std::fs::create_dir_all(&data_dir).unwrap();
    let file: Vec<u8> = (0..37u8).collect();
    let path = dir.path().join("2026-10-29_10-00-00.flac");
    std::fs::write(&path, &file).unwrap();
    let cfg = Arc::new(ConfigStore::open(&data_dir));
    cfg.set(BackendConfig {
        base_url: url.into(),
        api_key: key.map(str::to_string),
        job_options: opts,
        ..Default::default()
    })
    .unwrap();
    let store = Arc::new(Store::open(&data_dir.join("recordings.json")));
    let events = Arc::new(Mutex::new(Vec::new()));
    let ev = events.clone();
    let up = Uploader::new(
        store,
        cfg,
        Arc::new(move |r| ev.lock().unwrap().push(r.clone())),
    );
    up.add_finished(
        &Finished {
            path,
            stem: "2026-10-29_10-00-00".into(),
            size: file.len() as u64,
            duration: 1.0,
            channels: 2,
            created_at: Utc::now(),
        },
        true,
    );
    Env {
        _dir: dir,
        up,
        events,
        file,
        data_dir,
    }
}

const ID: &str = "2026-10-29_10-00-00";

fn state(e: &Env) -> UploadInfo {
    e.up.store().get(ID).unwrap().recording.upload
}

#[test]
fn full_flow_uploads_creates_per_channel_job_and_polls() {
    let srv = serve(10);
    let e = env(&srv.url, Some("sekret"), Some(serde_json::json!({})));
    e.up.step(ID); // upload + job
    let u = state(&e);
    assert_eq!(u.state, UploadState::Transcribing, "{u:?}");
    assert_eq!(u.upload_id.as_deref(), Some("u0"));
    assert_eq!(u.job_id.as_deref(), Some("j1"));
    e.up.step(ID); // running
    assert_eq!(state(&e).state, UploadState::Transcribing);
    e.up.step(ID); // succeeded
    let u = state(&e);
    assert_eq!(u.state, UploadState::Done);
    assert_eq!(u.transcript_id.as_deref(), Some("t-j1"));
    assert_eq!(u.attempts, 1);
    let m = srv.state.lock().unwrap();
    assert_eq!(m.uploads["u0"], e.file);
    assert_eq!(m.part_puts, vec![0, 1, 2, 3]);
    assert_eq!(
        m.jobs[0]["options"]["preprocess"]["channels"],
        "per_channel"
    );
    assert!(m.keys.iter().all(|k| k.as_deref() == Some("sekret")));
    let states: Vec<UploadState> = e
        .events
        .lock()
        .unwrap()
        .iter()
        .map(|r| r.upload.state)
        .collect();
    assert_eq!(
        states,
        vec![
            UploadState::Pending,
            UploadState::Uploading,
            UploadState::Uploaded,
            UploadState::Transcribing,
            UploadState::Done
        ]
    );
    // done entries are no longer scheduled
    assert!(e.up.store().next_due().is_none());
    // the recording is never deleted
    assert!(Path::new(&e.up.store().get(ID).unwrap().recording.path).exists());
}

#[test]
fn transient_failure_backs_off_and_resumes_after_restart() {
    let srv = serve(10);
    let e = env(&srv.url, None, Some(serde_json::json!({})));
    // init ok, part 0 ok, part 1 -> 503
    srv.state.lock().unwrap().inject_at = vec![(2, 503)];
    e.up.step(ID);
    let u = state(&e);
    assert_eq!(u.state, UploadState::Failed, "{u:?}");
    assert!(u.next_retry_at.is_some());
    assert!(u.error.as_deref().unwrap().contains("503"));
    let entry = e.up.store().get(ID).unwrap();
    assert_eq!(entry.session.as_ref().unwrap().next_part, 1);
    assert_eq!(entry.failures, 1);

    // "Restart": a new store / uploader from the same files; retry now.
    let cfg = Arc::new(ConfigStore::open(&e.data_dir));
    let store = Arc::new(Store::open(&e.data_dir.join("recordings.json")));
    let up2 = Uploader::new(store, cfg, Arc::new(|_| {}));
    up2.enqueue(ID).unwrap();
    up2.step(ID);
    let u = up2.store().get(ID).unwrap().recording.upload;
    assert_eq!(u.state, UploadState::Transcribing, "{u:?}");
    assert_eq!(u.attempts, 2);
    let m = srv.state.lock().unwrap();
    // part 0 was not sent again
    assert_eq!(m.part_puts, vec![0, 1, 2, 3]); // the failed PUT of part 1 never reached the handler
    assert!(srv.hits.load(Ordering::SeqCst) >= 7);
    assert_eq!(m.uploads["u0"], e.file);
}

#[test]
fn permanent_failure_waits_for_config_change() {
    let srv = serve(64);
    let e = env(&srv.url, None, None);
    srv.state.lock().unwrap().inject = vec![401];
    e.up.step(ID);
    let u = state(&e);
    assert_eq!(u.state, UploadState::Failed);
    assert_eq!(u.next_retry_at, None);
    assert!(e.up.store().next_due().is_none());
    e.up.config_changed();
    assert!(e.up.store().next_due().is_some());
    e.up.step(ID);
    // jobOptions null: upload only
    let u = state(&e);
    assert_eq!(u.state, UploadState::Uploaded);
    assert_eq!(u.job_id, None);
    assert!(srv.state.lock().unwrap().jobs.is_empty());
    assert!(e.up.store().next_due().is_none());
}

#[test]
fn backend_down_is_transient() {
    // Nothing listens on this port.
    let l = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", l.local_addr().unwrap());
    drop(l);
    let e = env(&url, None, Some(serde_json::json!({})));
    e.up.step(ID);
    let u = state(&e);
    assert_eq!(u.state, UploadState::Failed);
    assert!(u.next_retry_at.is_some());
    assert_eq!(u.attempts, 1);
}
