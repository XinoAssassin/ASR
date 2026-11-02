//! Bundled resources, the environment layout, and the command lines / environment variables of
//! the managed backend (pure functions, unit-tested on Linux).

use std::ffi::OsString;
use std::path::{Path, PathBuf};

use crate::ipc::ManagedHardware;

/// Resource folder (next to the exe) holding the bundled backend: `source\` (asr, server,
/// pyproject.toml, uv.lock, .python-version, LICENSE), `uv.exe` and `VERSION`.
pub const BUNDLE_DIR: &str = "managed-backend";
pub const PYTHON_VERSION: &str = "3.12";

pub fn exe_name(stem: &str) -> String {
    if cfg!(windows) {
        format!("{stem}.exe")
    } else {
        stem.to_string()
    }
}

/// The backend shipped with the "desktop + backend" edition.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Bundle {
    pub dir: PathBuf,
}

impl Bundle {
    /// The bundle under `resource_dir`, if this build has one (= the bundled edition).
    pub fn detect(resource_dir: &Path) -> Option<Self> {
        let b = Self {
            dir: resource_dir.join(BUNDLE_DIR),
        };
        (b.uv().is_file()
            && b.source().join("pyproject.toml").is_file()
            && b.source().join("uv.lock").is_file())
        .then_some(b)
    }

    pub fn uv(&self) -> PathBuf {
        self.dir.join(exe_name("uv"))
    }

    pub fn source(&self) -> PathBuf {
        self.dir.join("source")
    }

    /// Version of the bundled backend source (written by `scripts/stage-backend.py`:
    /// `<project version>+<content hash>`); "unknown" when missing.
    pub fn version(&self) -> String {
        std::fs::read_to_string(self.dir.join("VERSION"))
            .ok()
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "unknown".into())
    }
}

/// The managed environment under its root:
///
/// ```text
/// <root>\env-1\, env-2\   two slots, each a source copy (src\) + its venv (.venv\); one is
///                         active, a re-install / hardware switch builds the other one and
///                         swaps only on success (the old backend keeps working meanwhile)
/// <root>\uv-cache\       uv's cache (wheels are hard-linked into the venvs)
/// <root>\python\         the uv-managed CPython 3.12
/// ```
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Layout {
    pub root: PathBuf,
}

pub const SLOTS: [u8; 2] = [1, 2];

pub fn other_slot(n: u8) -> u8 {
    if n == 1 {
        2
    } else {
        1
    }
}

impl Layout {
    pub fn new(root: &Path) -> Self {
        Self {
            root: root.to_path_buf(),
        }
    }
    pub fn uv_cache(&self) -> PathBuf {
        self.root.join("uv-cache")
    }
    pub fn python_dir(&self) -> PathBuf {
        self.root.join("python")
    }
    pub fn slot(&self, n: u8) -> Slot {
        Slot {
            n,
            dir: self.root.join(format!("env-{n}")),
        }
    }
}

/// One environment slot: a copy of the bundled source (the uv project, installed editable)
/// and its venv.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Slot {
    pub n: u8,
    pub dir: PathBuf,
}

impl Slot {
    pub fn src(&self) -> PathBuf {
        self.dir.join("src")
    }
    pub fn venv(&self) -> PathBuf {
        self.dir.join(".venv")
    }
    fn venv_bin(&self) -> PathBuf {
        self.venv()
            .join(if cfg!(windows) { "Scripts" } else { "bin" })
    }
    pub fn venv_python(&self) -> PathBuf {
        self.venv_bin().join(exe_name("python"))
    }
    pub fn asr_exe(&self) -> PathBuf {
        self.venv_bin().join(exe_name("asr"))
    }
}

/// Variables for every uv call: everything uv writes stays under the environment root, and the
/// interpreter is always the uv-managed one there (never a system Python). Proxy variables are
/// inherited from the app's environment.
pub fn uv_env(l: &Layout, slot: &Slot) -> Vec<(OsString, OsString)> {
    vec![
        ("UV_CACHE_DIR".into(), l.uv_cache().into()),
        ("UV_PYTHON_INSTALL_DIR".into(), l.python_dir().into()),
        ("UV_PROJECT_ENVIRONMENT".into(), slot.venv().into()),
        ("UV_MANAGED_PYTHON".into(), "1".into()),
        ("UV_NO_PROGRESS".into(), "1".into()),
        ("NO_COLOR".into(), "1".into()),
        // A shell's activated venv must not redirect the sync.
        ("VIRTUAL_ENV".into(), OsString::new()),
    ]
}

/// `uv python install 3.12` (no shims in ~/.local/bin, no Windows registry entries).
pub fn python_install_args() -> Vec<String> {
    [
        "python",
        "install",
        PYTHON_VERSION,
        "--no-bin",
        "--no-registry",
    ]
    .map(String::from)
    .to_vec()
}

/// `uv sync` with the locked dependencies for `hw` + the local model stack.
pub fn sync_args(slot: &Slot, hw: ManagedHardware) -> Vec<String> {
    vec![
        "sync".into(),
        "--frozen".into(),
        "--no-dev".into(),
        "--extra".into(),
        hw.as_str().into(),
        "--extra".into(),
        "local".into(),
        "--managed-python".into(),
        "--python".into(),
        PYTHON_VERSION.into(),
        "--project".into(),
        slot.src().to_string_lossy().into_owned(),
    ]
}

/// Environment of the managed `asr serve`: data, uploads, models, device and the default
/// provider; the hub libraries' and MIOpen's caches go under the models folder (as in the
/// Docker images), so nothing lands in the user profile; loopback-only open mode (no key).
pub fn backend_env(hw: ManagedHardware, data: &Path, models: &Path) -> Vec<(String, String)> {
    let s = |p: PathBuf| p.to_string_lossy().into_owned();
    let cache = models.join(".cache");
    vec![
        ("ASR_DATA_DIR".into(), s(data.to_path_buf())),
        ("ASR_UPLOAD_ROOTS".into(), s(data.join("uploads"))),
        ("ASR_MODEL_CACHE_DIR".into(), s(models.to_path_buf())),
        ("ASR_DEVICE".into(), hw.as_str().into()),
        ("ASR_PROVIDER_DEFAULT".into(), "local".into()),
        ("ASR_API_KEY".into(), String::new()),
        ("MODELSCOPE_CACHE".into(), s(cache.join("modelscope"))),
        ("HF_HOME".into(), s(cache.join("huggingface"))),
        ("MIOPEN_USER_DB_PATH".into(), s(cache.join("miopen"))),
        ("MIOPEN_CUSTOM_CACHE_DIR".into(), s(cache.join("miopen"))),
        ("PYTHONUTF8".into(), "1".into()),
        ("PYTHONIOENCODING".into(), "utf-8".into()),
    ]
}

/// `asr serve` arguments (loopback only).
pub fn serve_args(port: u16) -> Vec<String> {
    vec![
        "serve".into(),
        "--host".into(),
        "127.0.0.1".into(),
        "--port".into(),
        port.to_string(),
    ]
}

/// Python run inside the new environment to check it: imports the stack and, for a GPU build,
/// requires a working device (a tiny kernel actually runs). Prints one `ASR-VERIFY {json}` line.
pub const VERIFY_SCRIPT: &str = r#"
import json, sys
want = sys.argv[1]
out = {"python": sys.version.split()[0], "ok": False}
try:
    import torch, transformers, funasr, server.cli  # noqa: F401
    out["torch"] = torch.__version__
    out["hip"] = getattr(torch.version, "hip", None)
    out["cuda"] = getattr(torch.version, "cuda", None)
    if want in ("cuda", "rocm"):
        if want == "rocm" and not out["hip"]:
            raise RuntimeError("torch is not a ROCm build")
        if want == "cuda" and not out["cuda"]:
            raise RuntimeError("torch is not a CUDA build")
        if not torch.cuda.is_available():
            raise RuntimeError("torch.cuda.is_available() is False (no usable GPU / driver)")
        out["device"] = torch.cuda.get_device_name(0)
        x = torch.arange(16, dtype=torch.float32, device="cuda")
        out["check"] = float((x * 2).sum().item())
        if out["check"] != 240.0:
            raise RuntimeError("GPU computed a wrong result")
    out["ok"] = True
except Exception as e:  # noqa: BLE001
    out["error"] = f"{type(e).__name__}: {e}"
print("ASR-VERIFY " + json.dumps(out), flush=True)
"#;

/// The verify result line, if this is one.
pub fn parse_verify(line: &str) -> Option<serde_json::Value> {
    serde_json::from_str(line.trim().strip_prefix("ASR-VERIFY ")?).ok()
}

/// Why the environment must be synced again before the backend starts, if it must:
/// another bundled backend version (app update), a moved portable folder (venvs hold absolute
/// paths), or a missing interpreter.
pub fn resync_reason(
    bundled_version: &str,
    installed_version: Option<&str>,
    installed_root: Option<&Path>,
    layout: &Layout,
    active: &Slot,
) -> Option<String> {
    if installed_version != Some(bundled_version) {
        return Some(format!(
            "bundled backend {bundled_version} differs from the installed {}",
            installed_version.unwrap_or("(none)")
        ));
    }
    if installed_root.is_some_and(|r| r != layout.root) {
        return Some(format!(
            "environment moved from {} to {}",
            installed_root.unwrap().display(),
            layout.root.display()
        ));
    }
    if !active.venv_python().is_file() || !active.asr_exe().is_file() {
        return Some("the Python environment is incomplete".into());
    }
    None
}

/// Total size of the files under `dirs` (best effort).
pub fn dir_size(dirs: &[PathBuf]) -> u64 {
    fn walk(p: &Path, total: &mut u64) {
        let Ok(rd) = std::fs::read_dir(p) else { return };
        for e in rd.flatten() {
            let Ok(ft) = e.file_type() else { continue };
            if ft.is_dir() {
                walk(&e.path(), total);
            } else if ft.is_file() {
                *total += e.metadata().map(|m| m.len()).unwrap_or(0);
            }
        }
    }
    let mut total = 0;
    for d in dirs {
        walk(d, &mut total);
    }
    total
}

/// Replace `dst` with a copy of `src` (directories recursively). Files are copied to a fresh
/// directory first, so a failed copy never leaves a half-updated tree in place.
pub fn replace_dir(src: &Path, dst: &Path) -> std::io::Result<()> {
    fn copy(src: &Path, dst: &Path) -> std::io::Result<()> {
        std::fs::create_dir_all(dst)?;
        for e in std::fs::read_dir(src)? {
            let e = e?;
            let to = dst.join(e.file_name());
            if e.file_type()?.is_dir() {
                copy(&e.path(), &to)?;
            } else {
                std::fs::copy(e.path(), &to)?;
            }
        }
        Ok(())
    }
    let tmp = dst.with_extension("new");
    remove_dir_retry(&tmp)?;
    copy(src, &tmp)?;
    remove_dir_retry(dst)?;
    std::fs::rename(&tmp, dst)
}

/// `remove_dir_all` that tolerates a missing folder and retries briefly (Windows: a process
/// that just exited may still hold handles; antivirus scanners open new files).
pub fn remove_dir_retry(p: &Path) -> std::io::Result<()> {
    let mut last = None;
    for i in 0..10 {
        match std::fs::remove_dir_all(p) {
            Ok(()) => return Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(e) => last = Some(e),
        }
        std::thread::sleep(std::time::Duration::from_millis(200 * (i + 1)));
    }
    Err(last.unwrap())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn layout_and_commands() {
        let l = Layout::new(Path::new("/x/ASR 录音/backend"));
        let s2 = l.slot(2);
        assert_eq!(s2.venv(), Path::new("/x/ASR 录音/backend/env-2/.venv"));
        assert_eq!(s2.src(), Path::new("/x/ASR 录音/backend/env-2/src"));
        assert!(s2.asr_exe().starts_with(s2.venv()));
        assert!(s2.venv_python().starts_with(s2.venv()));
        assert_eq!((other_slot(1), other_slot(2)), (2, 1));
        let env = uv_env(&l, &s2);
        let get = |k: &str| {
            env.iter()
                .find(|(n, _)| n == k)
                .map(|(_, v)| PathBuf::from(v))
                .unwrap()
        };
        assert_eq!(get("UV_CACHE_DIR"), l.uv_cache());
        assert_eq!(get("UV_PYTHON_INSTALL_DIR"), l.python_dir());
        assert_eq!(get("UV_PROJECT_ENVIRONMENT"), s2.venv());
        for d in [l.uv_cache(), l.python_dir(), s2.venv(), s2.src()] {
            assert!(d.starts_with(&l.root));
        }
        let a = python_install_args();
        assert!(a.contains(&"--no-bin".into()) && a.contains(&"--no-registry".into()));
        let s = sync_args(&s2, ManagedHardware::Rocm).join(" ");
        assert!(
            s.starts_with("sync --frozen --no-dev --extra rocm --extra local --managed-python"),
            "{s}"
        );
        assert!(
            s.ends_with(&format!("--project {}", s2.src().display())),
            "{s}"
        );
        assert_eq!(
            serve_args(8123),
            ["serve", "--host", "127.0.0.1", "--port", "8123"]
        );
    }

    #[test]
    fn backend_env_points_into_data_and_models() {
        let env = backend_env(
            ManagedHardware::Cpu,
            Path::new("/p/data/asr"),
            Path::new("/m/models"),
        );
        let get = |k: &str| env.iter().find(|(n, _)| n == k).unwrap().1.clone();
        assert_eq!(PathBuf::from(get("ASR_DATA_DIR")), Path::new("/p/data/asr"));
        assert_eq!(
            PathBuf::from(get("ASR_UPLOAD_ROOTS")),
            Path::new("/p/data/asr/uploads")
        );
        assert_eq!(
            PathBuf::from(get("ASR_MODEL_CACHE_DIR")),
            Path::new("/m/models")
        );
        assert_eq!(get("ASR_DEVICE"), "cpu");
        assert_eq!(get("ASR_PROVIDER_DEFAULT"), "local");
        assert_eq!(get("ASR_API_KEY"), "");
        assert!(PathBuf::from(get("HF_HOME")).starts_with("/m/models"));
        assert!(PathBuf::from(get("MODELSCOPE_CACHE")).starts_with("/m/models"));
    }

    #[test]
    fn version_and_relocation_trigger_a_resync() {
        let dir = tempfile::tempdir().unwrap();
        let l = Layout::new(&dir.path().join("backend"));
        let a = l.slot(1);
        // nothing installed
        assert!(resync_reason("0.1.0+a", None, None, &l, &a).is_some());
        // other version
        let r = resync_reason("0.1.0+b", Some("0.1.0+a"), Some(&l.root), &l, &a).unwrap();
        assert!(r.contains("0.1.0+b") && r.contains("0.1.0+a"), "{r}");
        // same version but no interpreter
        assert!(
            resync_reason("0.1.0+a", Some("0.1.0+a"), Some(&l.root), &l, &a)
                .unwrap()
                .contains("incomplete")
        );
        for f in [a.venv_python(), a.asr_exe()] {
            std::fs::create_dir_all(f.parent().unwrap()).unwrap();
            std::fs::write(f, b"").unwrap();
        }
        assert_eq!(
            resync_reason("0.1.0+a", Some("0.1.0+a"), Some(&l.root), &l, &a),
            None
        );
        // the folder moved
        let r = resync_reason("0.1.0+a", Some("0.1.0+a"), Some(Path::new("/old")), &l, &a).unwrap();
        assert!(r.contains("moved"), "{r}");
    }

    #[test]
    fn bundle_detection_and_version() {
        let dir = tempfile::tempdir().unwrap();
        assert!(Bundle::detect(dir.path()).is_none());
        let b = dir.path().join(BUNDLE_DIR);
        std::fs::create_dir_all(b.join("source")).unwrap();
        std::fs::write(b.join(exe_name("uv")), b"").unwrap();
        std::fs::write(b.join("source/pyproject.toml"), b"").unwrap();
        assert!(Bundle::detect(dir.path()).is_none(), "uv.lock missing");
        std::fs::write(b.join("source/uv.lock"), b"").unwrap();
        let bundle = Bundle::detect(dir.path()).unwrap();
        assert_eq!(bundle.version(), "unknown");
        std::fs::write(b.join("VERSION"), "0.1.0+abc\n").unwrap();
        assert_eq!(bundle.version(), "0.1.0+abc");
    }

    #[test]
    fn verify_line_and_dir_helpers() {
        assert_eq!(
            parse_verify(r#"ASR-VERIFY {"ok": true}"#).unwrap()["ok"],
            true
        );
        assert!(parse_verify("something else").is_none());
        let dir = tempfile::tempdir().unwrap();
        let src = dir.path().join("src");
        std::fs::create_dir_all(src.join("a/b")).unwrap();
        std::fs::write(src.join("a/b/f.py"), b"12345").unwrap();
        std::fs::write(src.join("top"), b"1").unwrap();
        let dst = dir.path().join("dst");
        std::fs::create_dir_all(&dst).unwrap();
        std::fs::write(dst.join("stale.py"), b"x").unwrap();
        replace_dir(&src, &dst).unwrap();
        assert!(!dst.join("stale.py").exists());
        assert_eq!(std::fs::read(dst.join("a/b/f.py")).unwrap(), b"12345");
        assert_eq!(dir_size(std::slice::from_ref(&dst)), 6);
        assert!(!dst.with_extension("new").exists());
        remove_dir_retry(&dst).unwrap();
        remove_dir_retry(&dst).unwrap(); // missing is fine
    }
}
