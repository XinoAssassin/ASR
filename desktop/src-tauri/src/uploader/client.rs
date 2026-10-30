//! Minimal blocking client for the backend's chunked upload and job endpoints.

use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;
use std::time::Duration;

use serde::Deserialize;
use serde_json::{json, Value};

use crate::ipc::BackendConfig;

#[derive(Debug, Clone, PartialEq)]
pub enum ApiError {
    /// Network trouble, 5xx, 408, 429: retry with backoff.
    Transient(String),
    /// Resource (upload session / upload) gone: restart that stage.
    NotFound(String),
    /// Request rejected (4xx): needs a config change or a manual retry.
    Permanent(String),
}

impl ApiError {
    pub fn message(&self) -> &str {
        match self {
            Self::Transient(m) | Self::NotFound(m) | Self::Permanent(m) => m,
        }
    }
}

#[derive(Debug, Deserialize)]
pub struct InitOut {
    pub session_id: String,
    pub part_size: u64,
    pub total_parts: u64,
}

#[derive(Debug, Deserialize)]
pub struct JobOut {
    pub id: String,
    pub status: String,
    #[serde(default)]
    pub error: Option<String>,
    #[serde(default)]
    pub transcript_id: Option<String>,
}

pub struct Client {
    http: reqwest::blocking::Client,
    base: String,
    key: Option<String>,
}

/// Keep error text short and free of secrets: drop URLs' query strings and anything after
/// "key"/"token"/"signature" markers.
pub fn redact(s: &str) -> String {
    let mut out: String = s
        .split_whitespace()
        .map(|w| {
            if w.contains("://") {
                w.split('?').next().unwrap_or("").to_string()
            } else {
                w.to_string()
            }
        })
        .collect::<Vec<_>>()
        .join(" ");
    for marker in ["api_key=", "apikey=", "token=", "signature=", "x-api-key:"] {
        if let Some(i) = out.to_ascii_lowercase().find(marker) {
            out.truncate(i + marker.len());
            out.push_str("***");
        }
    }
    if out.len() > 300 {
        let mut cut = 300;
        while !out.is_char_boundary(cut) {
            cut -= 1;
        }
        out.truncate(cut);
        out.push('…');
    }
    out
}

fn classify(status: reqwest::StatusCode, body: &str, location: Option<&str>) -> ApiError {
    if status.is_redirection() {
        // Redirects are not followed (the X-API-Key header must not travel to another host).
        let to = location
            .map(|l| redact(l.split('?').next().unwrap_or("")))
            .unwrap_or_else(|| "?".into());
        return ApiError::Permanent(format!(
            "HTTP {}: 后端返回了重定向（{to}），请把后端地址改为最终地址 / the backend redirected \
             to {to}; set the backend URL to the final address (redirects are not followed)",
            status.as_u16()
        ));
    }
    let detail = serde_json::from_str::<Value>(body)
        .ok()
        .and_then(|v| v.get("detail").cloned())
        .map(|d| match d {
            Value::String(s) => s,
            other => other.to_string(),
        })
        .unwrap_or_else(|| body.chars().take(200).collect());
    let msg = redact(&format!("HTTP {}: {detail}", status.as_u16()));
    match status.as_u16() {
        404 => ApiError::NotFound(msg),
        408 | 429 => ApiError::Transient(msg),
        s if s >= 500 => ApiError::Transient(msg),
        _ => ApiError::Permanent(msg),
    }
}

fn is_loopback(url: &str) -> bool {
    let rest = url.split("://").nth(1).unwrap_or("");
    let host = rest.split('/').next().unwrap_or("");
    let host = if host.starts_with('[') {
        host.split(']').next().unwrap_or("").trim_start_matches('[')
    } else {
        host.split(':').next().unwrap_or("")
    };
    host.eq_ignore_ascii_case("localhost") || host.starts_with("127.") || host == "::1"
}

fn net(e: reqwest::Error) -> ApiError {
    // reqwest's top-level text ("error sending request") hides the cause; append the chain.
    let e = e.without_url();
    let mut msg = e.to_string();
    let mut src = std::error::Error::source(&e);
    while let Some(s) = src {
        msg.push_str(": ");
        msg.push_str(&s.to_string());
        src = s.source();
    }
    ApiError::Transient(redact(&msg))
}

/// Timeout of one request without a large body.
const REQUEST_TIMEOUT: Duration = Duration::from_secs(300);
/// Slowest uplink a part upload must still complete on (64 kbit/s).
const MIN_UPLINK_BYTES_PER_S: u64 = 8 * 1024;

/// Timeout for sending `len` body bytes: [`REQUEST_TIMEOUT`] or the time the body takes at
/// [`MIN_UPLINK_BYTES_PER_S`] plus a minute, whichever is longer (a fixed timeout would fail the
/// same part forever on a slow line).
pub fn body_timeout(len: u64) -> Duration {
    REQUEST_TIMEOUT.max(Duration::from_secs(60 + len / MIN_UPLINK_BYTES_PER_S))
}

impl Client {
    pub fn new(cfg: &BackendConfig) -> Result<Self, String> {
        let mut b = reqwest::blocking::Client::builder()
            .connect_timeout(Duration::from_secs(10))
            .timeout(REQUEST_TIMEOUT)
            .user_agent(concat!("asr-recorder/", env!("CARGO_PKG_VERSION")))
            // Never follow redirects: reqwest would forward X-API-Key to the new host.
            .redirect(reqwest::redirect::Policy::none());
        if is_loopback(&cfg.base_url) {
            // Never route a same-machine backend through an env-configured proxy.
            b = b.no_proxy();
        }
        let http = b.build().map_err(|e| e.to_string())?;
        Ok(Self {
            http,
            base: cfg.base_url.trim_end_matches('/').to_string(),
            key: cfg.api_key.clone(),
        })
    }

    fn req(&self, method: reqwest::Method, path: &str) -> reqwest::blocking::RequestBuilder {
        let r = self.http.request(method, format!("{}{path}", self.base));
        match &self.key {
            Some(k) => r.header("X-API-Key", k),
            None => r,
        }
    }

    fn send<T: serde::de::DeserializeOwned>(
        &self,
        rb: reqwest::blocking::RequestBuilder,
    ) -> Result<T, ApiError> {
        self.send_with(rb, &[])
    }

    /// Like `send`, with extra status codes that count as transient for this endpoint.
    fn send_with<T: serde::de::DeserializeOwned>(
        &self,
        rb: reqwest::blocking::RequestBuilder,
        transient: &[u16],
    ) -> Result<T, ApiError> {
        let resp = rb.send().map_err(net)?;
        let status = resp.status();
        let location = resp
            .headers()
            .get(reqwest::header::LOCATION)
            .and_then(|v| v.to_str().ok())
            .map(str::to_string);
        let body = resp.text().map_err(net)?;
        if !status.is_success() {
            return Err(match classify(status, &body, location.as_deref()) {
                ApiError::Permanent(m) if transient.contains(&status.as_u16()) => {
                    ApiError::Transient(m)
                }
                e => e,
            });
        }
        serde_json::from_str(&body)
            .map_err(|e| ApiError::Transient(format!("unexpected response: {e}")))
    }

    pub fn init_upload(
        &self,
        filename: &str,
        size: u64,
        target_dir: &str,
    ) -> Result<InitOut, ApiError> {
        self.send(
            self.req(reqwest::Method::POST, "/api/uploads/init")
                .json(&json!({
                    "filename": filename,
                    "size": size,
                    "target_dir": target_dir,
                })),
        )
    }

    /// PUT part `n` (bytes `[n*part_size, ..)` of the file).
    pub fn put_part(
        &self,
        session: &str,
        n: u64,
        path: &Path,
        part_size: u64,
        size: u64,
    ) -> Result<(), ApiError> {
        let start = n * part_size;
        let len = part_size.min(size.saturating_sub(start));
        let mut buf = vec![0u8; len as usize];
        let mut f = File::open(path)
            .map_err(|e| ApiError::Permanent(format!("cannot read recording: {e}")))?;
        f.seek(SeekFrom::Start(start))
            .and_then(|_| f.read_exact(&mut buf))
            .map_err(|e| ApiError::Permanent(format!("cannot read recording: {e}")))?;
        let _: Value = self.send(
            self.req(
                reqwest::Method::PUT,
                &format!("/api/uploads/{session}/parts/{n}"),
            )
            .header(reqwest::header::CONTENT_TYPE, "application/octet-stream")
            .timeout(body_timeout(len))
            .body(buf),
        )?;
        Ok(())
    }

    /// Returns the upload id. The backend's `complete` is idempotent (re-completing a completed
    /// session returns the same upload); 409 means a concurrent duplicate is still assembling
    /// the file, so it is retried.
    pub fn complete(&self, session: &str) -> Result<String, ApiError> {
        let v: Value = self.send_with(
            self.req(
                reqwest::Method::POST,
                &format!("/api/uploads/{session}/complete"),
            ),
            &[409],
        )?;
        v.get("id")
            .and_then(Value::as_str)
            .map(str::to_string)
            .ok_or_else(|| ApiError::Transient("complete: response without id".into()))
    }

    /// `GET /api/uploads/{id}`: Ok when the upload exists, NotFound when it is gone.
    pub fn get_upload(&self, id: &str) -> Result<(), ApiError> {
        let _: Value = self.send(self.req(reqwest::Method::GET, &format!("/api/uploads/{id}")))?;
        Ok(())
    }

    pub fn create_job(&self, body: &Value) -> Result<JobOut, ApiError> {
        self.send(self.req(reqwest::Method::POST, "/api/jobs").json(body))
    }

    pub fn get_job(&self, id: &str) -> Result<JobOut, ApiError> {
        self.send(self.req(reqwest::Method::GET, &format!("/api/jobs/{id}")))
    }
}

/// `POST /api/jobs` body for an upload, per `docs/desktop-ipc.md`:
/// 2-channel files get `preprocess.channels = "per_channel"` unless the SPA chose an explicit
/// mode other than `auto`; mono files never send `per_channel` (the backend rejects it).
pub fn job_body(cfg: &BackendConfig, upload_id: &str, channels: u16) -> Option<Value> {
    let mut options = cfg.job_options.clone()?;
    if !options.is_object() {
        options = json!({});
    }
    let obj = options.as_object_mut().unwrap();
    let pre = obj.entry("preprocess").or_insert_with(|| json!({}));
    if !pre.is_object() {
        *pre = json!({});
    }
    let pre = pre.as_object_mut().unwrap();
    let current = pre
        .get("channels")
        .and_then(Value::as_str)
        .map(str::to_string);
    if channels >= 2 {
        if matches!(current.as_deref(), None | Some("auto")) {
            pre.insert("channels".into(), json!("per_channel"));
        }
    } else if current.as_deref() == Some("per_channel") {
        pre.insert("channels".into(), json!("auto"));
    }
    let mut body = json!({
        "upload_id": upload_id,
        "options": options,
        "term_set_ids": cfg.term_set_ids,
        "extra_terms": cfg.extra_terms,
    });
    if let Some(p) = cfg.provider {
        body["provider"] = serde_json::to_value(p).unwrap();
    }
    Some(body)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ipc::Provider;

    #[test]
    fn part_timeout_scales_with_the_body() {
        assert_eq!(body_timeout(0), REQUEST_TIMEOUT);
        assert_eq!(body_timeout(1024 * 1024), REQUEST_TIMEOUT);
        // an 8 MiB part gets the time it needs at 64 kbit/s (+ 1 min)
        assert_eq!(
            body_timeout(8 * 1024 * 1024),
            Duration::from_secs(60 + 1024)
        );
    }

    fn cfg(opts: Option<Value>) -> BackendConfig {
        BackendConfig {
            job_options: opts,
            provider: Some(Provider::Local),
            term_set_ids: vec!["ts1".into()],
            extra_terms: vec![json!({"text": "Kubernetes"})],
            ..Default::default()
        }
    }

    #[test]
    fn per_channel_forced_for_stereo_only() {
        let b = job_body(&cfg(Some(json!({}))), "u1", 2).unwrap();
        assert_eq!(b["options"]["preprocess"]["channels"], "per_channel");
        assert_eq!(b["provider"], "local");
        assert_eq!(b["upload_id"], "u1");
        assert_eq!(b["term_set_ids"][0], "ts1");
        assert_eq!(b["extra_terms"][0]["text"], "Kubernetes");
        let b = job_body(
            &cfg(Some(
                json!({"preprocess": {"channels": "auto", "peak_gain": false}}),
            )),
            "u",
            2,
        )
        .unwrap();
        assert_eq!(b["options"]["preprocess"]["channels"], "per_channel");
        assert_eq!(b["options"]["preprocess"]["peak_gain"], false);
        let b = job_body(
            &cfg(Some(json!({"preprocess": {"channels": "downmix"}}))),
            "u",
            2,
        )
        .unwrap();
        assert_eq!(b["options"]["preprocess"]["channels"], "downmix");
        // mono: per_channel -> auto, others untouched
        let b = job_body(
            &cfg(Some(json!({"preprocess": {"channels": "per_channel"}}))),
            "u",
            1,
        )
        .unwrap();
        assert_eq!(b["options"]["preprocess"]["channels"], "auto");
        let b = job_body(&cfg(Some(json!({}))), "u", 1).unwrap();
        assert!(b["options"]["preprocess"].get("channels").is_none());
        assert!(job_body(&cfg(None), "u", 2).is_none());
    }

    #[test]
    fn loopback_detection() {
        assert!(is_loopback("http://127.0.0.1:8000"));
        assert!(is_loopback("http://localhost/x"));
        assert!(is_loopback("http://[::1]:8000"));
        assert!(!is_loopback("https://asr.example.com"));
        assert!(!is_loopback("http://10.0.0.5:8000"));
    }

    #[test]
    fn redaction() {
        let r = redact("error sending request for url (http://h/api?token=abc) x-api-key: zzz");
        assert!(!r.contains("abc") && !r.contains("zzz"), "{r}");
        assert_eq!(
            classify(reqwest::StatusCode::NOT_FOUND, r#"{"detail":"gone"}"#, None),
            ApiError::NotFound("HTTP 404: gone".into())
        );
        assert!(matches!(
            classify(reqwest::StatusCode::BAD_GATEWAY, "", None),
            ApiError::Transient(_)
        ));
        assert!(matches!(
            classify(reqwest::StatusCode::UNAUTHORIZED, "{}", None),
            ApiError::Permanent(_)
        ));
        match classify(
            reqwest::StatusCode::TEMPORARY_REDIRECT,
            "",
            Some("https://other.example/api/jobs?token=abc"),
        ) {
            ApiError::Permanent(m) => {
                assert!(m.contains("https://other.example/api/jobs"), "{m}");
                assert!(!m.contains("abc"), "{m}");
            }
            other => panic!("{other:?}"),
        }
    }
}
