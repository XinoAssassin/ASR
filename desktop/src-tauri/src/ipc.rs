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

/// What is actually being captured (M10).
#[derive(Debug, Clone, Copy, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct Sources {
    pub system: bool,
    pub mic: bool,
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
    pub sources: Sources,
    /// Process tree being captured; None = whole render device.
    pub process_id: Option<u32>,
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
            sources: Sources::default(),
            process_id: None,
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
    /// Capture only this process tree (Windows process loopback); None = whole render device.
    #[serde(default)]
    pub process_id: Option<u32>,
}

impl Default for StartOptions {
    fn default() -> Self {
        Self {
            system: true,
            mic: true,
            output_device: None,
            input_device: None,
            out_dir: None,
            process_id: None,
        }
    }
}

/// A process with an audio session (M10).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct AudioProcess {
    pub pid: u32,
    pub name: String,
    pub title: Option<String>,
}

/// Optional local backend started by the app (M10).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct SidecarConfig {
    pub enabled: bool,
    pub command: String,
    pub args: Vec<String>,
    pub cwd: Option<String>,
    pub port: u16,
}

impl Default for SidecarConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            command: String::new(),
            args: vec!["serve".into(), "--port".into(), "8000".into()],
            cwd: None,
            port: 8000,
        }
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum SidecarState {
    Stopped,
    Starting,
    Running,
    Failed,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct SidecarStatus {
    pub state: SidecarState,
    pub pid: Option<u32>,
    pub url: Option<String>,
    pub error: Option<String>,
}

impl SidecarStatus {
    pub fn stopped() -> Self {
        Self {
            state: SidecarState::Stopped,
            pid: None,
            url: None,
            error: None,
        }
    }
}

/// Torch flavour of the managed backend ("desktop + backend" edition).
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Hash)]
#[serde(rename_all = "lowercase")]
pub enum ManagedHardware {
    Cpu,
    Cuda,
    Rocm,
}

impl ManagedHardware {
    pub const ALL: [ManagedHardware; 3] = [Self::Cpu, Self::Cuda, Self::Rocm];

    /// The `uv sync --extra` name and the backend's `ASR_DEVICE` value.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Cpu => "cpu",
            Self::Cuda => "cuda",
            Self::Rocm => "rocm",
        }
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum Edition {
    Bundled,
    External,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ManagedState {
    NotInstalled,
    Installing,
    Installed,
    Failed,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct ManagedBackendStatus {
    pub edition: Edition,
    pub state: ManagedState,
    pub hardware: Option<ManagedHardware>,
    pub backend_version: Option<String>,
    pub data_dir: Option<String>,
    pub models_dir: Option<String>,
    pub env_size_bytes: Option<u64>,
    /// Where dataDir / modelsDir go when not chosen (portable: inside the app folder).
    pub default_data_dir: Option<String>,
    pub default_models_dir: Option<String>,
    /// App state lives next to the exe (portable marker present).
    pub portable: bool,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct HardwareSuggestion {
    pub id: ManagedHardware,
    pub available: bool,
    pub recommended: bool,
    pub label: String,
    /// Why it is not available (None when it is).
    pub detail: Option<String>,
    /// Approximate one-time download for this choice.
    pub download_bytes: Option<u64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct ManagedInstallOptions {
    pub hardware: ManagedHardware,
    #[serde(default)]
    pub data_dir: Option<String>,
    #[serde(default)]
    pub models_dir: Option<String>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "lowercase")]
pub enum ManagedPhase {
    Prepare,
    Python,
    Packages,
    Finalize,
    Done,
    Failed,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct ManagedProgress {
    pub phase: ManagedPhase,
    pub message: String,
    pub percent: Option<f64>,
    pub log_line: Option<String>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum ProxyMode {
    System,
    None,
    Manual,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct ProxySettings {
    pub mode: ProxyMode,
    #[serde(default)]
    pub url: Option<String>,
    #[serde(default)]
    pub no_proxy: String,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum ProxySource {
    System,
    Env,
    Manual,
    None,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct EffectiveProxy {
    pub source: ProxySource,
    /// Credentials masked.
    pub url: Option<String>,
    pub no_proxy: String,
    pub pac_url: Option<String>,
    pub warnings: Vec<String>,
}

/// `network_get_proxy` / `network_set_proxy` result.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct ProxyState {
    pub settings: ProxySettings,
    pub effective: EffectiveProxy,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct ProxyTest {
    pub ok: bool,
    pub message: String,
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
        assert_eq!(
            s["sources"],
            serde_json::json!({"system": false, "mic": false})
        );
        assert!(s["processId"].is_null());
        // processId is optional in StartOptions
        let o: StartOptions = serde_json::from_value(
            serde_json::json!({"system": true, "mic": true, "processId": 42}),
        )
        .unwrap();
        assert_eq!(o.process_id, Some(42));
        let st = serde_json::to_value(SidecarStatus::stopped()).unwrap();
        assert_eq!(st["state"], "stopped");
        let c = serde_json::to_value(SidecarConfig::default()).unwrap();
        assert_eq!(c["args"][0], "serve");
        assert_eq!(c["port"], 8000);
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
    fn managed_payloads_match_the_contract() {
        let st = ManagedBackendStatus {
            edition: Edition::Bundled,
            state: ManagedState::NotInstalled,
            hardware: Some(ManagedHardware::Rocm),
            backend_version: None,
            data_dir: None,
            models_dir: None,
            env_size_bytes: Some(1),
            default_data_dir: Some("d".into()),
            default_models_dir: Some("m".into()),
            portable: true,
            error: None,
        };
        let v = serde_json::to_value(&st).unwrap();
        assert_eq!(v["edition"], "bundled");
        assert_eq!(v["state"], "not_installed");
        assert_eq!(v["hardware"], "rocm");
        for k in [
            "backendVersion",
            "dataDir",
            "modelsDir",
            "envSizeBytes",
            "defaultDataDir",
            "defaultModelsDir",
            "error",
        ] {
            assert!(v.get(k).is_some(), "{k}");
        }
        let o: ManagedInstallOptions =
            serde_json::from_value(serde_json::json!({"hardware": "cuda"})).unwrap();
        assert_eq!(o.hardware, ManagedHardware::Cuda);
        assert_eq!(o.data_dir, None);
        let p = serde_json::to_value(ManagedProgress {
            phase: ManagedPhase::Packages,
            message: "m".into(),
            percent: None,
            log_line: Some("l".into()),
        })
        .unwrap();
        assert_eq!(p["phase"], "packages");
        assert_eq!(p["logLine"], "l");
        assert!(p["percent"].is_null());
        let h = serde_json::to_value(HardwareSuggestion {
            id: ManagedHardware::Cpu,
            available: true,
            recommended: true,
            label: "CPU".into(),
            detail: None,
            download_bytes: Some(1_000_000_000),
        })
        .unwrap();
        assert_eq!(h["id"], "cpu");
        assert_eq!(h["downloadBytes"], 1_000_000_000u64);
        assert_eq!(v["portable"], true);
        assert!(h.get("detail").is_some());
    }

    #[test]
    fn proxy_payloads_match_the_contract() {
        let s: ProxySettings = serde_json::from_value(serde_json::json!({
            "mode": "manual", "url": "http://h:1", "noProxy": "localhost"
        }))
        .unwrap();
        assert_eq!(s.mode, ProxyMode::Manual);
        let v = serde_json::to_value(ProxyState {
            settings: s,
            effective: EffectiveProxy {
                source: ProxySource::Env,
                url: None,
                no_proxy: String::new(),
                pac_url: None,
                warnings: vec![],
            },
        })
        .unwrap();
        assert_eq!(v["settings"]["noProxy"], "localhost");
        assert_eq!(v["effective"]["source"], "env");
        assert!(v["effective"].get("pacUrl").is_some());
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
