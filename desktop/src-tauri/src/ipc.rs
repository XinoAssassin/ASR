//! Payload types of the frozen IPC contract (`docs/desktop-ipc.md`). serde `camelCase`.

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct Device {
    pub id: String,
    pub name: String,
    pub is_default: bool,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct DeviceList {
    pub outputs: Vec<Device>,
    pub inputs: Vec<Device>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum RecorderState {
    Idle,
    Recording,
    Finalizing,
}

#[derive(Debug, Clone, Copy, Default, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct Levels {
    pub system: f32,
    pub mic: f32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct RecorderStatus {
    pub state: RecorderState,
    pub elapsed: f64,
    pub levels: Levels,
    pub file: Option<String>,
    pub drift_ms: Option<f64>,
    pub warnings: Vec<String>,
}

impl RecorderStatus {
    pub fn idle(file: Option<String>) -> Self {
        Self {
            state: RecorderState::Idle,
            elapsed: 0.0,
            levels: Levels::default(),
            file,
            drift_ms: None,
            warnings: Vec::new(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct StartOptions {
    pub system: bool,
    pub mic: bool,
    #[serde(default)]
    pub output_device: Option<String>,
    #[serde(default)]
    pub input_device: Option<String>,
    #[serde(default)]
    pub out_dir: Option<String>,
}

impl Default for StartOptions {
    fn default() -> Self {
        Self {
            system: true,
            mic: true,
            output_device: None,
            input_device: None,
            out_dir: None,
        }
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum UploadState {
    Pending,
    Uploading,
    Uploaded,
    Transcribing,
    Done,
    Failed,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct UploadInfo {
    pub state: UploadState,
    pub attempts: u32,
    pub upload_id: Option<String>,
    pub job_id: Option<String>,
    /// Set once the job succeeded.
    #[serde(default)]
    pub transcript_id: Option<String>,
    pub error: Option<String>,
    pub next_retry_at: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct Recording {
    pub id: String,
    pub path: String,
    pub name: String,
    pub size: u64,
    pub duration: f64,
    pub created_at: String,
    pub upload: UploadInfo,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Provider {
    Local,
    Cloud,
}

#[derive(Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct BackendConfig {
    pub base_url: String,
    pub api_key: Option<String>,
    pub target_dir: String,
    pub auto_upload: bool,
    pub job_options: Option<serde_json::Value>,
    pub provider: Option<Provider>,
    /// body.term_set_ids for POST /api/jobs.
    #[serde(default)]
    pub term_set_ids: Vec<String>,
    /// body.extra_terms (Term objects).
    #[serde(default)]
    pub extra_terms: Vec<serde_json::Value>,
}

impl std::fmt::Debug for BackendConfig {
    // Never print the key (AGENTS rule 3).
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("BackendConfig")
            .field("base_url", &self.base_url)
            .field("api_key", &self.api_key.as_ref().map(|_| "***"))
            .field("target_dir", &self.target_dir)
            .field("auto_upload", &self.auto_upload)
            .field("job_options", &self.job_options)
            .field("provider", &self.provider)
            .field("term_set_ids", &self.term_set_ids)
            .field("extra_terms", &self.extra_terms.len())
            .finish()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn payloads_are_camel_case() {
        let s = serde_json::to_value(RecorderStatus::idle(None)).unwrap();
        assert_eq!(s["state"], "idle");
        assert!(s.get("driftMs").is_some());
        let o: StartOptions = serde_json::from_value(serde_json::json!({
            "system": true, "mic": false, "outputDevice": null, "inputDevice": "x", "outDir": null
        }))
        .unwrap();
        assert_eq!(o.input_device.as_deref(), Some("x"));
        let u = serde_json::to_value(UploadInfo {
            state: UploadState::Transcribing,
            attempts: 1,
            upload_id: None,
            job_id: None,
            transcript_id: None,
            error: None,
            next_retry_at: None,
        })
        .unwrap();
        assert_eq!(u["state"], "transcribing");
        assert!(u.get("transcriptId").is_some());
        assert!(u.get("nextRetryAt").is_some());
    }

    #[test]
    fn old_config_without_terms_loads() {
        let c: BackendConfig = serde_json::from_value(serde_json::json!({
            "baseUrl": "http://x", "apiKey": null, "targetDir": "", "autoUpload": true,
            "jobOptions": null, "provider": null
        }))
        .unwrap();
        assert!(c.term_set_ids.is_empty() && c.extra_terms.is_empty());
        let v = serde_json::to_value(&c).unwrap();
        assert!(v.get("termSetIds").is_some() && v.get("extraTerms").is_some());
    }

    #[test]
    fn debug_masks_key() {
        let c = BackendConfig {
            base_url: "http://x".into(),
            api_key: Some("secret-value".into()),
            target_dir: String::new(),
            auto_upload: true,
            job_options: None,
            provider: None,
            term_set_ids: vec![],
            extra_terms: vec![],
        };
        assert!(!format!("{c:?}").contains("secret-value"));
    }
}
