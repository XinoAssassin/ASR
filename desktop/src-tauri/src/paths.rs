//! Where the app keeps its state. One resolution for everything (config store, upload queue,
//! sidecar, managed backend, logs, WebView profile):
//!
//! * **Portable mode**: a marker file `portable` (or `portable.txt`) next to the exe. Everything
//!   lives under that folder (the *root*), so the unpacked zip is self-contained:
//!
//!   ```text
//!   <root>\data\app\       backend.json, api-key, settings.json, recordings.json, sidecar.json, managed.json
//!   <root>\data\logs\      app log, sidecar.log, managed-backend.log
//!   <root>\data\webview\   WebView2 profile (the SPA's local storage)
//!   <root>\data\asr\       ASR_DATA_DIR of the managed backend (asr.db, uploads\)
//!   <root>\recordings\     default recordings folder
//!   <root>\models\         ASR_MODEL_CACHE_DIR of the managed backend (overridable)
//!   <root>\backend\        managed Python environment: src\, .venv\, uv-cache\, python\
//!   ```
//!
//! * **Per-user mode** (no marker; dev runs and installed builds): the OS folders, as before.
//!   `%APPDATA%\dev.asr.recorder` (app data), `%LOCALAPPDATA%\dev.asr.recorder\logs`, the
//!   WebView profile where Tauri puts it, `Documents\ASR Recordings`, and for the managed backend
//!   `%LOCALAPPDATA%\dev.asr.recorder\{backend,data,models}`.
//!
//! A portable folder can be moved (another drive letter, a USB stick): paths stored under the
//! old root are rebased onto the new one at the next start ([`rebase`]).

use std::path::{Path, PathBuf};

/// Marker files that switch on portable mode (next to the exe).
pub const PORTABLE_MARKERS: [&str; 2] = ["portable", "portable.txt"];

/// The portable root (the exe's folder) when a marker file sits next to `exe`.
pub fn portable_root(exe: &Path) -> Option<PathBuf> {
    let dir = exe.parent()?;
    PORTABLE_MARKERS
        .iter()
        .any(|m| dir.join(m).is_file())
        .then(|| dir.to_path_buf())
}

/// The OS per-user folders (from Tauri's path resolver) used without the portable marker.
#[derive(Debug, Clone)]
pub struct PerUserDirs {
    /// Roaming app data (`%APPDATA%\<identifier>`).
    pub app_data: PathBuf,
    /// Local app data (`%LOCALAPPDATA%\<identifier>`).
    pub app_local_data: PathBuf,
    pub app_log: PathBuf,
    pub documents: Option<PathBuf>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AppPaths {
    /// Some in portable mode.
    pub portable_root: Option<PathBuf>,
    /// Config, API key, upload queue, sidecar / managed-backend state.
    pub app_data: PathBuf,
    pub logs: PathBuf,
    /// WebView2 profile; None = Tauri's default (per-user mode).
    pub webview: Option<PathBuf>,
    /// Default recordings folder.
    pub recordings: PathBuf,
    /// Managed backend environment root (source copy, `.venv`, uv cache, uv-managed Python).
    pub managed_root: PathBuf,
    /// Default `ASR_DATA_DIR` of the managed backend.
    pub managed_data: PathBuf,
    /// Default `ASR_MODEL_CACHE_DIR` of the managed backend.
    pub models: PathBuf,
}

impl AppPaths {
    pub fn portable(root: &Path) -> Self {
        let data = root.join("data");
        Self {
            portable_root: Some(root.to_path_buf()),
            app_data: data.join("app"),
            logs: data.join("logs"),
            webview: Some(data.join("webview")),
            recordings: root.join("recordings"),
            managed_root: root.join("backend"),
            managed_data: data.join("asr"),
            models: root.join("models"),
        }
    }

    pub fn per_user(d: &PerUserDirs) -> Self {
        Self {
            portable_root: None,
            app_data: d.app_data.clone(),
            logs: d.app_log.clone(),
            webview: None,
            recordings: d
                .documents
                .clone()
                .unwrap_or_else(|| d.app_data.clone())
                .join("ASR Recordings"),
            managed_root: d.app_local_data.join("backend"),
            managed_data: d.app_local_data.join("data"),
            models: d.app_local_data.join("models"),
        }
    }

    /// Portable when the marker sits next to `exe`, else the per-user folders (asked lazily).
    pub fn resolve(exe: &Path, per_user: impl FnOnce() -> PerUserDirs) -> Self {
        match portable_root(exe) {
            Some(root) => Self::portable(&root),
            None => Self::per_user(&per_user()),
        }
    }

    pub fn is_portable(&self) -> bool {
        self.portable_root.is_some()
    }
}

/// Suffix of the app identifier in portable mode (`dev.asr.recorder.portable-<key>`): each
/// portable folder is its own single instance, independent of an installed copy or another
/// folder. FNV-1a of the case-folded root path.
pub fn instance_key(root: &Path) -> String {
    let mut h: u32 = 0x811c_9dc5;
    for b in root.to_string_lossy().to_lowercase().bytes() {
        h ^= u32::from(b);
        h = h.wrapping_mul(0x0100_0193);
    }
    format!("{h:08x}")
}

/// `\\?\D:\x` → `D:\x` (Tauri's resource dir is a verbatim path on Windows; tools and logs
/// are happier with the plain form). UNC and other verbatim forms are left alone.
pub fn strip_verbatim(p: &Path) -> PathBuf {
    let s = p.to_string_lossy();
    match s.strip_prefix(r"\\?\") {
        Some(rest) if rest.as_bytes().get(1) == Some(&b':') => PathBuf::from(rest),
        _ => p.to_path_buf(),
    }
}

/// `p` moved from under `old_root` to under `new_root`; None if `p` is not under `old_root`.
/// Windows paths compare case-insensitively (a drive letter may change case).
pub fn rebase(p: &Path, old_root: &Path, new_root: &Path) -> Option<PathBuf> {
    if let Ok(rest) = p.strip_prefix(old_root) {
        return Some(new_root.join(rest));
    }
    if cfg!(windows) {
        let (ps, os) = (p.to_string_lossy(), old_root.to_string_lossy());
        let os = os.trim_end_matches(['\\', '/']);
        if ps.len() > os.len()
            && ps.is_char_boundary(os.len())
            && ps[..os.len()].eq_ignore_ascii_case(os)
            && matches!(ps.as_bytes()[os.len()], b'\\' | b'/')
        {
            return Some(new_root.join(&ps[os.len() + 1..]));
        }
    }
    None
}

/// [`rebase`] for a stored string path; unchanged when not under `old_root`.
pub fn rebase_str(p: &str, old_root: &Path, new_root: &Path) -> String {
    rebase(Path::new(p), old_root, new_root)
        .map(|r| r.to_string_lossy().into_owned())
        .unwrap_or_else(|| p.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn marker_switches_to_portable_layout() {
        let dir = tempfile::tempdir().unwrap();
        // spaces and CJK in the folder name
        let root = dir.path().join("ASR 录音 portable");
        std::fs::create_dir_all(&root).unwrap();
        let exe = root.join("asr-recorder.exe");
        let per_user = || PerUserDirs {
            app_data: PathBuf::from("/roaming/dev.asr.recorder"),
            app_local_data: PathBuf::from("/local/dev.asr.recorder"),
            app_log: PathBuf::from("/local/dev.asr.recorder/logs"),
            documents: Some(PathBuf::from("/docs")),
        };
        let p = AppPaths::resolve(&exe, per_user);
        assert!(!p.is_portable());
        assert_eq!(p.app_data, PathBuf::from("/roaming/dev.asr.recorder"));
        assert_eq!(p.recordings, PathBuf::from("/docs/ASR Recordings"));
        assert_eq!(
            p.managed_root,
            PathBuf::from("/local/dev.asr.recorder/backend")
        );
        assert_eq!(p.models, PathBuf::from("/local/dev.asr.recorder/models"));
        assert_eq!(p.webview, None);

        for marker in PORTABLE_MARKERS {
            std::fs::write(root.join(marker), b"").unwrap();
            let p = AppPaths::resolve(&exe, || unreachable!("per-user dirs not needed"));
            assert_eq!(p.portable_root.as_deref(), Some(root.as_path()));
            assert_eq!(p.app_data, root.join("data").join("app"));
            assert_eq!(p.logs, root.join("data").join("logs"));
            assert_eq!(p.webview, Some(root.join("data").join("webview")));
            assert_eq!(p.recordings, root.join("recordings"));
            assert_eq!(p.managed_root, root.join("backend"));
            assert_eq!(p.managed_data, root.join("data").join("asr"));
            assert_eq!(p.models, root.join("models"));
            std::fs::remove_file(root.join(marker)).unwrap();
        }
        // a folder named "portable" is not a marker
        std::fs::create_dir(root.join("portable")).unwrap();
        assert!(portable_root(&exe).is_none());
    }

    #[test]
    fn verbatim_drive_paths_are_simplified() {
        assert_eq!(
            strip_verbatim(Path::new(r"\\?\D:\ASR 录音\x")),
            PathBuf::from(r"D:\ASR 录音\x")
        );
        assert_eq!(
            strip_verbatim(Path::new(r"\\?\UNC\srv\share")),
            PathBuf::from(r"\\?\UNC\srv\share")
        );
        assert_eq!(strip_verbatim(Path::new("/a/b")), PathBuf::from("/a/b"));
    }

    #[test]
    fn instance_key_is_stable_per_folder() {
        let a = instance_key(Path::new("/x/ASR"));
        assert_eq!(a.len(), 8);
        assert_eq!(a, instance_key(Path::new("/X/asr")));
        assert_ne!(a, instance_key(Path::new("/x/ASR 2")));
    }

    #[test]
    fn rebase_moves_paths_under_the_old_root_only() {
        let old = Path::new("/mnt/e/ASR");
        let new = Path::new("/media/usb/ASR 2");
        assert_eq!(
            rebase(Path::new("/mnt/e/ASR/recordings/a.flac"), old, new),
            Some(PathBuf::from("/media/usb/ASR 2/recordings/a.flac"))
        );
        assert_eq!(rebase(Path::new("/mnt/e/ASRX/a"), old, new), None);
        assert_eq!(rebase(Path::new("/elsewhere/a"), old, new), None);
        assert_eq!(rebase_str("/elsewhere/a", old, new), "/elsewhere/a");
        assert_eq!(
            PathBuf::from(rebase_str("/mnt/e/ASR/backend/.venv", old, new)),
            Path::new("/media/usb/ASR 2/backend/.venv")
        );
    }

    #[cfg(windows)]
    #[test]
    fn rebase_ignores_drive_letter_case() {
        assert_eq!(
            rebase(
                Path::new(r"e:\ASR\models\x"),
                Path::new(r"E:\ASR"),
                Path::new(r"F:\Tools\ASR")
            ),
            Some(PathBuf::from(r"F:\Tools\ASR\models\x"))
        );
    }
}
