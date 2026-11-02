//! Managed backend of the "desktop + backend" edition (`docs/desktop-ipc.md`): the backend
//! source and `uv` ship as resources; on first run `managed_backend_install` creates a Python
//! environment for the chosen hardware (cpu / cuda / rocm), verifies it, writes the sidecar
//! config and starts `asr serve` on loopback with the managed data / models folders.
//!
//! The environment has two slots (`<root>\env-1`, `<root>\env-2`, see [`env::Layout`]); one is
//! active. Steps of an install (one background thread, cancellable):
//! 1. `prepare`: create the folders, wipe the target slot (the inactive one when an environment
//!    is installed, so the current backend keeps running) and copy the bundled source into it.
//! 2. `python`: `uv python install 3.12` into `<root>\python` (no shims, no registry entries).
//! 3. `packages`: `uv sync --frozen --no-dev --extra <hw> --extra local` into the slot's
//!    `.venv`, cache in `<root>\uv-cache`.
//! 4. `finalize`: import torch / transformers / funasr in the new environment and, for a GPU
//!    build, run a kernel on the GPU (an unusable GPU is a failure, never a silent CPU
//!    fallback); then stop the old backend, make the new slot active, remove the old slot and
//!    start the sidecar. `installed` is reported only after that start was attempted (a failed
//!    start shows in `sidecar://status`).
//!
//! Cancel or failure removes the partial slot; data and models are kept, and an environment
//! that was installed before stays installed (and keeps running).

pub mod env;
pub mod hardware;
pub mod progress;

#[cfg(all(test, unix))]
mod tests;

use std::ffi::OsString;
use std::io::BufRead;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};

use crate::ipc::{
    Edition, HardwareSuggestion, ManagedBackendStatus, ManagedHardware, ManagedInstallOptions,
    ManagedPhase, ManagedProgress, ManagedState, SidecarConfig, SidecarState, SidecarStatus,
};
use crate::sidecar::{LogFile, ManagedLaunch, Sidecar};
use env::{Bundle, Layout, Slot};
use progress::Tracker;

pub type OnProgress = Arc<dyn Fn(&ManagedProgress) + Send + Sync>;
pub type OnStatus = Arc<dyn Fn(&ManagedBackendStatus) + Send + Sync>;

const VERIFY_TIMEOUT: Duration = Duration::from_secs(600);
const LOG_TAIL: usize = 8;

/// `managed.json` in the app data dir.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Persisted {
    /// A verified environment exists in `active_slot`.
    #[serde(default)]
    pub installed: bool,
    #[serde(default)]
    pub active_slot: Option<u8>,
    /// Hardware the active environment was built for.
    #[serde(default)]
    pub hardware: Option<ManagedHardware>,
    /// Bundled backend version the active environment was synced with.
    #[serde(default)]
    pub backend_version: Option<String>,
    /// Chosen data / models folders; None = the default for this mode.
    #[serde(default)]
    pub data_dir: Option<PathBuf>,
    #[serde(default)]
    pub models_dir: Option<PathBuf>,
    #[serde(default)]
    pub env_size_bytes: Option<u64>,
    /// Environment root at the last sync (a moved portable folder needs a new venv).
    #[serde(default)]
    pub env_root: Option<PathBuf>,
    /// Last setup failure (kept until the next successful setup).
    #[serde(default)]
    pub error: Option<String>,
}

/// Folders of the managed backend (from [`crate::paths::AppPaths`]).
#[derive(Debug, Clone)]
pub struct ManagedPaths {
    /// Holds `managed.json`.
    pub app_data: PathBuf,
    /// Gets `managed-backend.log`.
    pub logs: PathBuf,
    pub root: PathBuf,
    pub default_data: PathBuf,
    pub default_models: PathBuf,
    pub portable: bool,
}

/// Why a run ended early.
enum Stop {
    Cancelled,
    Failed(String),
}

impl From<String> for Stop {
    fn from(s: String) -> Self {
        Stop::Failed(s)
    }
}

/// A spawned tool and its process tree.
struct Tree {
    child: Child,
    #[cfg(windows)]
    job: Option<crate::sidecar::win::Job>,
}

impl Tree {
    fn kill(&mut self) {
        #[cfg(windows)]
        if let Some(job) = self.job.as_ref() {
            job.terminate();
        }
        #[cfg(unix)]
        // SAFETY: kill(2) on our own child's process group.
        unsafe {
            libc::kill(-(self.child.id() as i32), libc::SIGKILL);
        }
        let _ = self.child.kill();
    }
}

pub struct Managed {
    layout: Layout,
    bundle: Option<Bundle>,
    paths: ManagedPaths,
    state_path: PathBuf,
    persisted: Mutex<Persisted>,
    installing: AtomicBool,
    cancel: AtomicBool,
    child: Mutex<Option<Tree>>,
    worker: Mutex<Option<std::thread::JoinHandle<()>>>,
    sidecar: Arc<Sidecar>,
    on_progress: OnProgress,
    on_status: OnStatus,
    /// Start the sidecar after an install (tests without a server switch it off).
    start_sidecar: AtomicBool,
    proxy: Mutex<Option<crate::proxy::ProxyFn>>,
}

fn read_persisted(p: &Path) -> Persisted {
    std::fs::read(p)
        .ok()
        .and_then(|b| serde_json::from_slice(&b).ok())
        .unwrap_or_default()
}

fn free_port() -> Option<u16> {
    std::net::TcpListener::bind(("127.0.0.1", 0))
        .ok()?
        .local_addr()
        .ok()
        .map(|a| a.port())
}

fn port_free(port: u16) -> bool {
    std::net::TcpListener::bind(("127.0.0.1", port)).is_ok()
}

/// A folder option: None / empty = `fallback`; otherwise it must be absolute.
fn chosen_dir(opt: Option<&str>, fallback: Option<PathBuf>) -> Result<Option<PathBuf>, String> {
    match opt.map(str::trim).filter(|s| !s.is_empty()) {
        None => Ok(fallback),
        Some(s) => {
            let p = PathBuf::from(s);
            if !p.is_absolute() {
                return Err(format!("folder must be an absolute path: {s}"));
            }
            Ok(Some(p))
        }
    }
}

/// Which folder `managed_backend_reveal` opens.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum RevealTarget {
    Data,
    Models,
    Backend,
}

impl Managed {
    pub fn new(
        paths: ManagedPaths,
        bundle: Option<Bundle>,
        sidecar: Arc<Sidecar>,
        on_progress: OnProgress,
        on_status: OnStatus,
    ) -> Arc<Self> {
        let state_path = paths.app_data.join("managed.json");
        let persisted = read_persisted(&state_path);
        let me = Arc::new(Self {
            layout: Layout::new(&paths.root),
            bundle,
            paths,
            state_path,
            persisted: Mutex::new(persisted),
            installing: AtomicBool::new(false),
            cancel: AtomicBool::new(false),
            child: Mutex::new(None),
            worker: Mutex::new(None),
            sidecar,
            on_progress,
            on_status,
            start_sidecar: AtomicBool::new(true),
            proxy: Mutex::new(None),
        });
        me.register_launch();
        me
    }

    #[cfg(all(test, unix))]
    fn without_sidecar_start(self: Arc<Self>) -> Arc<Self> {
        self.start_sidecar.store(false, Ordering::SeqCst);
        self
    }

    /// The app's proxy setting, applied to every tool of a setup (uv downloads).
    pub fn set_proxy(&self, p: crate::proxy::ProxyFn) {
        *crate::util::lock(&self.proxy) = Some(p);
    }

    pub fn edition(&self) -> Edition {
        if self.bundle.is_some() {
            Edition::Bundled
        } else {
            Edition::External
        }
    }

    pub fn layout(&self) -> &Layout {
        &self.layout
    }

    /// The slot of the installed environment.
    pub fn active_slot(&self) -> Option<Slot> {
        let p = self.persisted();
        p.installed
            .then_some(p.active_slot)
            .flatten()
            .map(|n| self.layout.slot(n))
    }

    fn persisted(&self) -> Persisted {
        crate::util::lock(&self.persisted).clone()
    }

    fn update(&self, f: impl FnOnce(&mut Persisted)) {
        let mut p = crate::util::lock(&self.persisted);
        f(&mut p);
        match serde_json::to_vec_pretty(&*p) {
            Ok(b) => {
                if let Err(e) = crate::util::write_atomic(&self.state_path, &b) {
                    log::warn!(
                        "managed backend: cannot store {}: {e}",
                        self.state_path.display()
                    );
                }
            }
            Err(e) => log::warn!("managed backend: cannot serialise state: {e}"),
        }
    }

    fn data_dir(&self, p: &Persisted) -> PathBuf {
        p.data_dir
            .clone()
            .unwrap_or_else(|| self.paths.default_data.clone())
    }

    fn models_dir(&self, p: &Persisted) -> PathBuf {
        p.models_dir
            .clone()
            .unwrap_or_else(|| self.paths.default_models.clone())
    }

    /// Paths stored under a moved portable root follow it (the env root deliberately does not:
    /// the mismatch triggers the venv rebuild).
    pub fn rebase(&self, old_root: &Path, new_root: &Path) {
        let p = self.persisted();
        let rb = |d: &Option<PathBuf>| {
            d.as_ref()
                .map(|d| crate::paths::rebase(d, old_root, new_root).unwrap_or_else(|| d.clone()))
        };
        let (data, models) = (rb(&p.data_dir), rb(&p.models_dir));
        if data != p.data_dir || models != p.models_dir {
            self.update(|p| {
                p.data_dir = data;
                p.models_dir = models;
            });
        }
        self.register_launch();
    }

    /// Tell the sidecar which command is the managed backend and its environment.
    fn register_launch(&self) {
        let p = self.persisted();
        let (Some(slot), Some(hw), true) = (self.active_slot(), p.hardware, self.bundle.is_some())
        else {
            self.sidecar.set_managed_launch(None);
            return;
        };
        self.sidecar.set_managed_launch(Some(ManagedLaunch {
            command: slot.asr_exe(),
            env: env::backend_env(hw, &self.data_dir(&p), &self.models_dir(&p)),
        }));
    }

    /// The sidecar is configured to run a managed backend (either slot).
    pub fn sidecar_is_managed(&self) -> bool {
        let cmd = self.sidecar.config().command;
        env::SLOTS
            .iter()
            .any(|n| Path::new(cmd.trim()) == self.layout.slot(*n).asr_exe())
    }

    pub fn status(&self) -> ManagedBackendStatus {
        let p = self.persisted();
        let s = |p: PathBuf| Some(p.to_string_lossy().into_owned());
        let Some(bundle) = self.bundle.as_ref() else {
            return ManagedBackendStatus {
                edition: Edition::External,
                state: ManagedState::NotInstalled,
                hardware: None,
                backend_version: None,
                data_dir: None,
                models_dir: None,
                env_size_bytes: None,
                default_data_dir: None,
                default_models_dir: None,
                portable: self.paths.portable,
                error: None,
            };
        };
        let state = if self.installing.load(Ordering::SeqCst) {
            ManagedState::Installing
        } else if p.installed {
            ManagedState::Installed
        } else if p.error.is_some() {
            ManagedState::Failed
        } else {
            ManagedState::NotInstalled
        };
        ManagedBackendStatus {
            edition: Edition::Bundled,
            state,
            hardware: p.hardware,
            backend_version: Some(if p.installed {
                p.backend_version
                    .clone()
                    .unwrap_or_else(|| bundle.version())
            } else {
                bundle.version()
            }),
            data_dir: s(self.data_dir(&p)),
            models_dir: s(self.models_dir(&p)),
            env_size_bytes: p.installed.then_some(p.env_size_bytes).flatten(),
            default_data_dir: s(self.paths.default_data.clone()),
            default_models_dir: s(self.paths.default_models.clone()),
            portable: self.paths.portable,
            error: p.error.clone(),
        }
    }

    fn emit_status(&self) {
        (self.on_status)(&self.status());
    }

    pub fn detect(&self) -> Vec<HardwareSuggestion> {
        let gpus = hardware::detect_gpus();
        log::info!("managed backend: display adapters {gpus:?}");
        hardware::suggest(&gpus)
    }

    /// The folder `managed_backend_reveal` opens (created when missing).
    pub fn reveal_dir(&self, which: RevealTarget) -> Result<PathBuf, String> {
        if self.bundle.is_none() {
            return Err("这个版本不含内置后端 / This build has no bundled backend".into());
        }
        let p = self.persisted();
        let dir = match which {
            RevealTarget::Data => self.data_dir(&p),
            RevealTarget::Models => self.models_dir(&p),
            RevealTarget::Backend => self.layout.root.clone(),
        };
        std::fs::create_dir_all(&dir).map_err(|e| format!("{}: {e}", dir.display()))?;
        Ok(dir)
    }

    /// Start (or re-run) the setup in the background.
    pub fn install(
        self: &Arc<Self>,
        opts: ManagedInstallOptions,
    ) -> Result<ManagedBackendStatus, String> {
        if self.bundle.is_none() {
            return Err("这个版本不含内置后端 / This build has no bundled backend".into());
        }
        let p = self.persisted();
        // None = keep the earlier choice (default when there is none); a path = use it.
        let data = chosen_dir(opts.data_dir.as_deref(), p.data_dir.clone())?;
        let models = chosen_dir(opts.models_dir.as_deref(), p.models_dir.clone())?;
        self.begin(opts.hardware, data, models, true)?;
        Ok(self.status())
    }

    fn begin(
        self: &Arc<Self>,
        hw: ManagedHardware,
        data: Option<PathBuf>,
        models: Option<PathBuf>,
        configure_sidecar: bool,
    ) -> Result<(), String> {
        if self.installing.swap(true, Ordering::SeqCst) {
            return Err("正在安装中 / Setup is already running".into());
        }
        // A finished worker's handle (the thread has ended).
        if let Some(h) = crate::util::lock(&self.worker).take() {
            let _ = h.join();
        }
        self.cancel.store(false, Ordering::SeqCst);
        self.update(|p| {
            p.data_dir = data;
            p.models_dir = models;
        });
        self.register_launch();
        self.emit_status();
        let me = self.clone();
        let h = std::thread::Builder::new()
            .name("managed-install".into())
            .spawn(move || me.run(hw, configure_sidecar))
            .map_err(|e| {
                self.installing.store(false, Ordering::SeqCst);
                format!("cannot start the setup thread: {e}")
            })?;
        *crate::util::lock(&self.worker) = Some(h);
        Ok(())
    }

    /// Stop a running setup; returns once it has cleaned up.
    pub fn cancel(&self) -> ManagedBackendStatus {
        if self.installing.load(Ordering::SeqCst) {
            self.cancel.store(true, Ordering::SeqCst);
            if let Some(t) = crate::util::lock(&self.child).as_mut() {
                t.kill();
            }
            let h = crate::util::lock(&self.worker).take();
            if let Some(h) = h {
                let _ = h.join();
            }
        }
        self.status()
    }

    /// At app start (bundled edition): re-sync an installed environment whose bundled version
    /// or location changed, then start the managed sidecar. Returns true if it takes care of
    /// starting the sidecar (the caller must not start it as well).
    pub fn startup(self: &Arc<Self>) -> bool {
        let Some(bundle) = self.bundle.as_ref() else {
            return false;
        };
        let p = self.persisted();
        let (Some(slot), Some(hw)) = (self.active_slot(), p.hardware) else {
            return false;
        };
        let managed = self.sidecar_is_managed();
        let enabled = self.sidecar.config().enabled;
        if let Some(reason) = env::resync_reason(
            &bundle.version(),
            p.backend_version.as_deref(),
            p.env_root.as_deref(),
            &self.layout,
            &slot,
        ) {
            log::info!("managed backend: re-syncing the environment ({reason})");
            self.log_line(&format!("re-sync at start: {reason}"));
            if let Err(e) = self.begin(hw, p.data_dir, p.models_dir, managed && enabled) {
                log::warn!("managed backend: re-sync not started: {e}");
                return false;
            }
            return managed;
        }
        if managed && enabled {
            let me = self.clone();
            std::thread::spawn(move || {
                let st = me.start_sidecar();
                log::info!("managed sidecar on launch: {:?}", st.state);
            });
            return true;
        }
        false
    }

    /// Write the managed sidecar config (a free loopback port) and start it.
    pub fn start_sidecar(&self) -> SidecarStatus {
        self.register_launch();
        let failed = |e: String| SidecarStatus {
            state: SidecarState::Failed,
            pid: None,
            url: None,
            error: Some(e),
        };
        let Some(slot) = self.active_slot() else {
            return failed("the local backend is not installed".into());
        };
        let old = self.sidecar.config();
        let port = if self.sidecar_is_managed() && port_free(old.port) {
            old.port
        } else {
            match free_port() {
                Some(p) => p,
                None => return failed("no free local port".into()),
            }
        };
        let cfg = SidecarConfig {
            enabled: true,
            command: slot.asr_exe().to_string_lossy().into_owned(),
            args: env::serve_args(port),
            cwd: Some(self.layout.root.to_string_lossy().into_owned()),
            port,
        };
        if let Err(e) = self.sidecar.set_config(cfg) {
            return failed(e);
        }
        self.sidecar.start()
    }

    fn log_line(&self, line: &str) {
        LogFile {
            path: self.paths.logs.join("managed-backend.log"),
        }
        .write_line(line);
    }

    fn progress(&self, mut p: ManagedProgress) {
        p.percent = p.percent.map(|v| (v * 100.0).round() / 100.0);
        (self.on_progress)(&p);
    }

    // ------------------------------------------------------------------------------ worker
    fn run(self: Arc<Self>, hw: ManagedHardware, configure_sidecar: bool) {
        let started = Instant::now();
        let before = self.active_slot();
        let target = self.layout.slot(match before.as_ref() {
            Some(s) => env::other_slot(s.n),
            None => self.persisted().active_slot.unwrap_or(1),
        });
        self.log_line(&format!(
            "setup started: hardware {}, building {}",
            hw.as_str(),
            target.dir.display()
        ));
        let mut tracker = Tracker::new();
        let res = self.steps(hw, &target, configure_sidecar, &mut tracker);
        *crate::util::lock(&self.child) = None;
        let secs = started.elapsed().as_secs();
        let failed = match res {
            Ok(()) => {
                self.log_line(&format!("setup done in {secs} s"));
                self.installing.store(false, Ordering::SeqCst);
                self.progress(ManagedProgress {
                    phase: ManagedPhase::Done,
                    message: progress::phase_message(ManagedPhase::Done).into(),
                    percent: Some(100.0),
                    log_line: None,
                });
                self.emit_status();
                return;
            }
            Err(Stop::Cancelled) => {
                self.log_line(&format!("setup cancelled after {secs} s"));
                None
            }
            Err(Stop::Failed(msg)) => {
                self.log_line(&format!("setup failed after {secs} s: {msg}"));
                Some(msg)
            }
        };
        // Remove the partial slot; the previous environment (if any) stays active.
        if let Err(e) = env::remove_dir_retry(&target.dir) {
            log::warn!("managed backend: cannot remove the partial environment: {e}");
        }
        self.update(|p| {
            if before.is_none() {
                p.installed = false;
                p.env_size_bytes = None;
                if failed.is_some() {
                    p.hardware = Some(hw);
                }
            }
            // failed: the redacted reason; cancelled: no error
            p.error = failed.clone();
        });
        self.register_launch();
        self.installing.store(false, Ordering::SeqCst);
        self.progress(ManagedProgress {
            phase: ManagedPhase::Failed,
            message: failed.unwrap_or_else(|| "已取消 / Cancelled".into()),
            percent: Some(tracker.percent()),
            log_line: None,
        });
        self.emit_status();
    }

    fn check_cancel(&self) -> Result<(), Stop> {
        if self.cancel.load(Ordering::SeqCst) {
            Err(Stop::Cancelled)
        } else {
            Ok(())
        }
    }

    fn steps(
        &self,
        hw: ManagedHardware,
        target: &Slot,
        configure_sidecar: bool,
        t: &mut Tracker,
    ) -> Result<(), Stop> {
        let bundle = self
            .bundle
            .clone()
            .ok_or_else(|| Stop::Failed("no bundle".into()))?;
        let l = self.layout.clone();
        let p = self.persisted();
        let (data, models) = (self.data_dir(&p), self.models_dir(&p));

        // 1. prepare
        self.progress(t.phase(ManagedPhase::Prepare, None));
        for d in [&l.root, &data.join("uploads"), &models] {
            std::fs::create_dir_all(d)
                .map_err(|e| format!("cannot create {}: {e}", d.display()))?;
        }
        env::remove_dir_retry(&target.dir)
            .map_err(|e| format!("cannot clear {} (in use?): {e}", target.dir.display()))?;
        env::replace_dir(&bundle.source(), &target.src()).map_err(|e| {
            format!(
                "cannot copy the backend source to {}: {e}",
                target.src().display()
            )
        })?;
        self.check_cancel()?;

        // 2. python
        self.progress(t.phase(ManagedPhase::Python, None));
        let uv = bundle.uv();
        let uv_env = env::uv_env(&l, target);
        self.run_tool(
            &uv,
            &env::python_install_args(),
            &uv_env,
            ManagedPhase::Python,
            t,
            None,
        )?;
        self.check_cancel()?;

        // 3. packages
        self.progress(t.phase(ManagedPhase::Packages, None));
        self.run_tool(
            &uv,
            &env::sync_args(target, hw),
            &uv_env,
            ManagedPhase::Packages,
            t,
            None,
        )?;
        self.check_cancel()?;

        // 4. finalize: verify
        self.progress(t.phase(ManagedPhase::Finalize, None));
        let mut venv_env: Vec<(OsString, OsString)> = env::backend_env(hw, &data, &models)
            .into_iter()
            .map(|(k, v)| (k.into(), v.into()))
            .collect();
        venv_env.push(("VIRTUAL_ENV".into(), target.venv().into()));
        let lines = self.run_tool(
            &target.venv_python(),
            &["-c".into(), env::VERIFY_SCRIPT.into(), hw.as_str().into()],
            &venv_env,
            ManagedPhase::Finalize,
            t,
            Some(VERIFY_TIMEOUT),
        )?;
        let verdict = lines
            .iter()
            .rev()
            .find_map(|l| env::parse_verify(l))
            .ok_or_else(|| {
                "环境验证没有结果 / The environment check printed no result".to_string()
            })?;
        if verdict["ok"] != true {
            let err = verdict["error"].as_str().unwrap_or("unknown error");
            return Err(Stop::Failed(match hw {
                ManagedHardware::Cpu => format!("环境验证失败 / Environment check failed: {err}"),
                _ => format!(
                    "{} 不可用 / The GPU is not usable with the {} build: {err}. \
                     请更新显卡驱动或改用 CPU / Update the GPU driver or choose CPU.",
                    hw.as_str().to_uppercase(),
                    hw.as_str()
                ),
            }));
        }
        if !target.asr_exe().is_file() {
            return Err(Stop::Failed(format!(
                "{} is missing after the setup",
                target.asr_exe().display()
            )));
        }
        self.log_line(&format!("verified: {verdict}"));
        self.check_cancel()?;

        // swap: stop the old backend, activate the new slot, drop the old one
        let was_running =
            self.sidecar_is_managed() && self.sidecar.status().state != SidecarState::Stopped;
        if was_running {
            self.log_line("stopping the previous backend for the swap");
            self.sidecar.stop();
        }
        let size = env::dir_size(&[target.venv(), l.python_dir()]);
        let old = p.active_slot.filter(|n| *n != target.n);
        self.update(|p| {
            p.installed = true;
            p.active_slot = Some(target.n);
            p.hardware = Some(hw);
            p.backend_version = Some(bundle.version());
            p.env_root = Some(l.root.clone());
            p.env_size_bytes = Some(size);
            p.error = None;
        });
        self.register_launch();
        if let Some(n) = old {
            let dir = l.slot(n).dir;
            if let Err(e) = env::remove_dir_retry(&dir) {
                // Removed when that slot is built next.
                log::warn!("managed backend: cannot remove {}: {e}", dir.display());
            }
        }

        // start the backend (a failed start is reported through sidecar://status)
        if (configure_sidecar || was_running) && self.start_sidecar.load(Ordering::SeqCst) {
            self.progress(ManagedProgress {
                phase: ManagedPhase::Finalize,
                message: "正在启动本地后端 / Starting the local backend".into(),
                percent: Some(t.percent().max(95.0)),
                log_line: None,
            });
            let st = self.start_sidecar();
            self.log_line(&match st.state {
                SidecarState::Running => {
                    format!("sidecar running at {}", st.url.unwrap_or_default())
                }
                _ => format!("sidecar did not start: {:?} {:?}", st.state, st.error),
            });
        }
        Ok(())
    }

    /// Run one tool to completion, streaming its output into progress events and the log.
    /// Returns its output lines.
    fn run_tool(
        &self,
        program: &Path,
        args: &[String],
        envs: &[(OsString, OsString)],
        phase: ManagedPhase,
        t: &mut Tracker,
        timeout: Option<Duration>,
    ) -> Result<Vec<String>, Stop> {
        let mut cmd = Command::new(program);
        cmd.args(args)
            .envs(envs.iter().map(|(k, v)| (k, v)))
            .current_dir(&self.layout.root)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        let proxy = crate::util::lock(&self.proxy).clone();
        if let Some(p) = proxy {
            let r = p();
            self.log_line(&format!(
                "proxy: {:?} {}",
                r.effective.source,
                r.effective.url.as_deref().unwrap_or("-")
            ));
            r.apply(&mut cmd);
        }
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            cmd.process_group(0);
        }
        let shown = args
            .iter()
            .map(|a| if a.len() > 80 { "<script>" } else { a.as_str() })
            .collect::<Vec<_>>()
            .join(" ");
        self.log_line(&format!("run: {} {shown}", program.display()));
        let mut child = cmd
            .spawn()
            .map_err(|e| format!("cannot start {}: {e}", program.display()))?;
        let (tx, rx) = mpsc::channel::<String>();
        let mut readers = Vec::new();
        for stream in [
            child
                .stdout
                .take()
                .map(|s| Box::new(s) as Box<dyn std::io::Read + Send>),
            child
                .stderr
                .take()
                .map(|s| Box::new(s) as Box<dyn std::io::Read + Send>),
        ]
        .into_iter()
        .flatten()
        {
            let tx = tx.clone();
            readers.push(std::thread::spawn(move || {
                let mut r = std::io::BufReader::new(stream);
                let mut buf = Vec::new();
                loop {
                    buf.clear();
                    match r.read_until(b'\n', &mut buf) {
                        Ok(0) | Err(_) => break,
                        Ok(_) => {
                            let s = String::from_utf8_lossy(&buf);
                            for part in s.split(['\r', '\n']).filter(|p| !p.trim().is_empty()) {
                                if tx.send(part.to_string()).is_err() {
                                    return;
                                }
                            }
                        }
                    }
                }
            }));
        }
        drop(tx);
        *crate::util::lock(&self.child) = Some(Tree {
            #[cfg(windows)]
            job: crate::sidecar::win::Job::assign(&child),
            child,
        });
        if self.cancel.load(Ordering::SeqCst) {
            if let Some(tr) = crate::util::lock(&self.child).as_mut() {
                tr.kill();
            }
        }
        let deadline = timeout.map(|d| Instant::now() + d);
        let mut lines = Vec::new();
        let mut timed_out = false;
        loop {
            match rx.recv_timeout(Duration::from_millis(250)) {
                Ok(line) => {
                    let p = t.line(phase, &line);
                    if let Some(l) = p.log_line.as_deref() {
                        self.log_line(&format!("[{}] {l}", phase_name(phase)));
                    }
                    self.progress(p);
                    lines.push(line);
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {}
                Err(mpsc::RecvTimeoutError::Disconnected) => break,
            }
            if deadline.is_some_and(|d| Instant::now() >= d) && !timed_out {
                timed_out = true;
                if let Some(tr) = crate::util::lock(&self.child).as_mut() {
                    tr.kill();
                }
            }
        }
        for r in readers {
            let _ = r.join();
        }
        let status = {
            let mut c = crate::util::lock(&self.child);
            let st = c.as_mut().map(|tr| tr.child.wait());
            // Also take down anything the tool left behind (Windows: the job; the Unix
            // process group is not signalled after the wait, its id may be reused).
            #[cfg(windows)]
            if let Some(job) = c.as_ref().and_then(|tr| tr.job.as_ref()) {
                job.terminate();
            }
            *c = None;
            st
        };
        self.check_cancel()?;
        if timed_out {
            return Err(Stop::Failed(format!(
                "{} did not finish in {} s",
                program.display(),
                timeout.unwrap_or_default().as_secs()
            )));
        }
        match status {
            Some(Ok(s)) if s.success() => Ok(lines),
            other => {
                let code = match other {
                    Some(Ok(s)) => s.to_string(),
                    Some(Err(e)) => e.to_string(),
                    None => "no status".into(),
                };
                let tail: Vec<String> = lines
                    .iter()
                    .rev()
                    .filter(|l| !l.trim_start().starts_with('+'))
                    .take(LOG_TAIL)
                    .map(|l| progress::redact_line(l))
                    .collect::<Vec<_>>()
                    .into_iter()
                    .rev()
                    .collect();
                Err(Stop::Failed(format!(
                    "{}失败 / {} failed ({code}): {}",
                    phase_label_zh(phase),
                    phase_name(phase),
                    tail.join(" | ")
                )))
            }
        }
    }
}

fn phase_name(p: ManagedPhase) -> &'static str {
    match p {
        ManagedPhase::Prepare => "prepare",
        ManagedPhase::Python => "python",
        ManagedPhase::Packages => "packages",
        ManagedPhase::Finalize => "finalize",
        ManagedPhase::Done => "done",
        ManagedPhase::Failed => "failed",
    }
}

fn phase_label_zh(p: ManagedPhase) -> &'static str {
    match p {
        ManagedPhase::Python => "安装 Python ",
        ManagedPhase::Packages => "安装依赖",
        ManagedPhase::Finalize => "环境验证",
        _ => "",
    }
}
