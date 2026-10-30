//! Backend connection config, persisted in the app data dir. The API key lives in its own file
//! (`api-key`) and leaves this module only masked as `"***"` (AGENTS rule 3).

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use crate::ipc::{BackendConfig, StartOptions};

pub const MASK: &str = "***";

impl Default for BackendConfig {
    fn default() -> Self {
        Self {
            base_url: "http://127.0.0.1:8000".into(),
            api_key: None,
            target_dir: "recordings".into(),
            auto_upload: true,
            job_options: Some(serde_json::json!({})),
            provider: None,
            term_set_ids: Vec::new(),
            extra_terms: Vec::new(),
        }
    }
}

/// Persisted app settings besides the backend config.
#[derive(Debug, Clone, Default, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct LocalSettings {
    /// Options of the last `recorder_start` (used by the tray / hotkey toggle).
    #[serde(default)]
    pub last_start: Option<StartOptions>,
    /// Partial WAV being written; set while recording, for crash recovery.
    #[serde(default)]
    pub active_partial: Option<PathBuf>,
    /// Output folders used so far (scanned for orphaned partial files at startup).
    #[serde(default)]
    pub out_dirs: Vec<PathBuf>,
}

pub struct ConfigStore {
    dir: PathBuf,
    config: Mutex<BackendConfig>,
    key: Mutex<Option<String>>,
    settings: Mutex<LocalSettings>,
}

fn read_json<T: serde::de::DeserializeOwned + Default>(p: &Path) -> T {
    match std::fs::read(p) {
        Ok(b) => serde_json::from_slice(&b).unwrap_or_else(|e| {
            log::warn!("ignoring unreadable {}: {e}", p.display());
            T::default()
        }),
        Err(_) => T::default(),
    }
}

pub fn normalize_base_url(url: &str) -> Result<String, String> {
    let u = url.trim().trim_end_matches('/');
    if !(u.starts_with("http://") || u.starts_with("https://")) || u.len() < 9 {
        return Err("baseUrl must be an absolute http(s) URL".into());
    }
    Ok(u.to_string())
}

/// Key files written by older versions were world-readable on Unix: tighten them to 0600.
fn restrict_key_file(p: &Path) {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if let Ok(m) = std::fs::metadata(p) {
            if m.permissions().mode() & 0o077 != 0 {
                if let Err(e) = std::fs::set_permissions(p, std::fs::Permissions::from_mode(0o600))
                {
                    log::warn!("cannot restrict {}: {e}", p.display());
                }
            }
        }
    }
    #[cfg(not(unix))]
    let _ = p;
}

impl ConfigStore {
    pub fn open(dir: &Path) -> Self {
        let mut config: BackendConfig = read_json(&dir.join("backend.json"));
        config.api_key = None;
        restrict_key_file(&dir.join("api-key"));
        let key = std::fs::read_to_string(dir.join("api-key"))
            .ok()
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty());
        Self {
            dir: dir.to_path_buf(),
            config: Mutex::new(config),
            key: Mutex::new(key),
            settings: Mutex::new(read_json(&dir.join("settings.json"))),
        }
    }

    /// Config with the real key (internal use: the uploader).
    pub fn get_with_key(&self) -> BackendConfig {
        let mut c = crate::util::lock(&self.config).clone();
        c.api_key = crate::util::lock(&self.key).clone();
        c
    }

    /// Config as returned to the SPA (key masked).
    pub fn get_masked(&self) -> BackendConfig {
        let mut c = crate::util::lock(&self.config).clone();
        c.api_key = crate::util::lock(&self.key)
            .as_ref()
            .map(|_| MASK.to_string());
        c
    }

    pub fn set(&self, mut new: BackendConfig) -> Result<BackendConfig, String> {
        new.base_url = normalize_base_url(&new.base_url)?;
        match new.api_key.take() {
            Some(k) if k == MASK => {}
            Some(k) if !k.trim().is_empty() => {
                let k = k.trim().to_string();
                crate::util::write_atomic_private(&self.dir.join("api-key"), k.as_bytes())
                    .map_err(|e| format!("cannot store the API key: {e}"))?;
                *crate::util::lock(&self.key) = Some(k);
            }
            _ => {
                let _ = std::fs::remove_file(self.dir.join("api-key"));
                *crate::util::lock(&self.key) = None;
            }
        }
        let body = serde_json::to_vec_pretty(&new).map_err(|e| e.to_string())?;
        crate::util::write_atomic(&self.dir.join("backend.json"), &body)
            .map_err(|e| format!("cannot store the config: {e}"))?;
        *crate::util::lock(&self.config) = new;
        Ok(self.get_masked())
    }

    pub fn settings(&self) -> LocalSettings {
        crate::util::lock(&self.settings).clone()
    }

    pub fn update_settings(&self, f: impl FnOnce(&mut LocalSettings)) {
        let mut s = crate::util::lock(&self.settings);
        f(&mut s);
        match serde_json::to_vec_pretty(&*s) {
            Ok(b) => {
                if let Err(e) = crate::util::write_atomic(&self.dir.join("settings.json"), &b) {
                    log::warn!("cannot store settings: {e}");
                }
            }
            Err(e) => log::warn!("cannot serialise settings: {e}"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn key_is_masked_kept_and_cleared() {
        let dir = tempfile::tempdir().unwrap();
        let s = ConfigStore::open(dir.path());
        assert_eq!(s.get_masked().api_key, None);
        let mut c = BackendConfig {
            base_url: "http://h:8000/".into(),
            api_key: Some("k-123".into()),
            ..Default::default()
        };
        let out = s.set(c.clone()).unwrap();
        assert_eq!(out.api_key.as_deref(), Some(MASK));
        assert_eq!(out.base_url, "http://h:8000");
        let stored = std::fs::read_to_string(dir.path().join("backend.json")).unwrap();
        assert!(!stored.contains("k-123"));
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let m = std::fs::metadata(dir.path().join("api-key")).unwrap();
            assert_eq!(m.permissions().mode() & 0o777, 0o600);
        }
        // "***" keeps the key, also across a reopen
        c.api_key = Some(MASK.into());
        s.set(c.clone()).unwrap();
        let s2 = ConfigStore::open(dir.path());
        assert_eq!(s2.get_with_key().api_key.as_deref(), Some("k-123"));
        // null clears it
        c.api_key = None;
        s2.set(c).unwrap();
        assert_eq!(s2.get_with_key().api_key, None);
        assert!(!dir.path().join("api-key").exists());
    }

    #[cfg(unix)]
    #[test]
    fn old_world_readable_key_file_is_restricted() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("api-key");
        std::fs::write(&p, "k-old\n").unwrap();
        std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o644)).unwrap();
        let s = ConfigStore::open(dir.path());
        assert_eq!(s.get_with_key().api_key.as_deref(), Some("k-old"));
        let mode = std::fs::metadata(&p).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600);
    }

    #[test]
    fn rejects_bad_url() {
        let dir = tempfile::tempdir().unwrap();
        let s = ConfigStore::open(dir.path());
        let c = BackendConfig {
            base_url: "ftp://x".into(),
            ..Default::default()
        };
        assert!(s.set(c).is_err());
    }
}
