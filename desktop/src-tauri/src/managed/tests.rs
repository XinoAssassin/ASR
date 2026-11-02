//! Install / cancel / re-sync against a fake `uv` (shell script) on Unix; the backend is the
//! sidecar's Python stand-in server.

use super::*;
use crate::ipc::SidecarStatus;

const FAKE_UV: &str = r#"#!/bin/sh
here="$(dirname "$0")"
mode="$(cat "$here/mode" 2>/dev/null)"
echo "$* | cache=$UV_CACHE_DIR | py=$UV_PYTHON_INSTALL_DIR | env=$UV_PROJECT_ENVIRONMENT | proxy=$HTTPS_PROXY | no_proxy=$NO_PROXY" >> "$here/calls.log"
case "$1" in
python)
  echo "Downloading cpython-3.12.12-linux-x86_64-gnu (download) (21.1MiB)"
  mkdir -p "$UV_PYTHON_INSTALL_DIR/cpython-3.12.12"
  echo "Installed Python 3.12.12 in 0.01s"
  ;;
sync)
  echo "Using CPython 3.12.12"
  echo "Creating virtual environment at: $UV_PROJECT_ENVIRONMENT"
  echo "Downloading torch (198.9MiB)"
  if [ "$mode" = "slow" ]; then sleep 30; fi
  if [ "$mode" = "fail" ]; then
    echo "error: Failed to fetch https://user:s3cret@mirror.example/simple/torch/?sig=abc" >&2
    exit 2
  fi
  echo " Downloaded torch"
  echo "Prepared 1 package in 0.02s"
  mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"
  cp "$here/fake-python" "$UV_PROJECT_ENVIRONMENT/bin/python"
  cp "$here/fake-asr" "$UV_PROJECT_ENVIRONMENT/bin/asr"
  chmod +x "$UV_PROJECT_ENVIRONMENT/bin/python" "$UV_PROJECT_ENVIRONMENT/bin/asr"
  echo "Installed 1 package in 0.01s"
  echo " + torch==2.14.1+cpu"
  ;;
esac
"#;

/// `python -c SCRIPT <hw>`: $3 is the hardware.
const FAKE_PYTHON: &str = r#"#!/bin/sh
mode="$(cat "__BUNDLE__/mode" 2>/dev/null)"
if [ "$mode" = "gpu-fail" ] && [ "$3" != "cpu" ]; then
  echo 'ASR-VERIFY {"ok": false, "error": "RuntimeError: torch.cuda.is_available() is False"}'
else
  echo "ASR-VERIFY {\"ok\": true, \"want\": \"$3\", \"device\": \"$ASR_DEVICE\"}"
fi
"#;

/// `asr serve --host 127.0.0.1 --port N`: the stand-in health server, printing its env.
const FAKE_ASR: &str = r#"#!/bin/sh
exec python3 "__BUNDLE__/standin.py" "$5"
"#;

const STANDIN: &str = r#"
import http.server, os, sys
for k in ("ASR_DEVICE", "ASR_DATA_DIR", "ASR_UPLOAD_ROOTS", "ASR_MODEL_CACHE_DIR", "ASR_PROVIDER_DEFAULT", "ASR_CORS_ORIGINS"):
    print(k + "=" + os.environ.get(k, "<unset>"), flush=True)
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        ok = self.path == "/api/health"
        self.send_response(200 if ok else 404); self.end_headers()
        self.wfile.write(b'{"status":"ok"}' if ok else b'')
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
"#;

fn have_python3() -> bool {
    Command::new("python3")
        .arg("--version")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

struct Fixture {
    _dir: tempfile::TempDir,
    base: PathBuf,
    bundle_dir: PathBuf,
    paths: ManagedPaths,
    progress: Arc<Mutex<Vec<ManagedProgress>>>,
    statuses: Arc<Mutex<Vec<ManagedBackendStatus>>>,
    sidecar: Arc<Sidecar>,
}

fn write_exec(p: &Path, body: &str) {
    use std::os::unix::fs::PermissionsExt;
    std::fs::write(p, body).unwrap();
    std::fs::set_permissions(p, std::fs::Permissions::from_mode(0o755)).unwrap();
}

fn fixture(with_bundle: bool) -> Fixture {
    let dir = tempfile::tempdir().unwrap();
    // spaces and CJK, like a portable folder on a user's desktop
    let base = dir.path().join("ASR 录音");
    let res = base.join("res");
    let bundle_dir = res.join(env::BUNDLE_DIR);
    if with_bundle {
        let src = bundle_dir.join("source");
        std::fs::create_dir_all(src.join("server")).unwrap();
        std::fs::write(src.join("pyproject.toml"), "[project]\nname='asr'\n").unwrap();
        std::fs::write(src.join("uv.lock"), "version = 1\n").unwrap();
        std::fs::write(src.join("server/cli.py"), "# cli\n").unwrap();
        std::fs::write(bundle_dir.join("VERSION"), "0.1.0+aaa\n").unwrap();
        write_exec(&bundle_dir.join("uv"), FAKE_UV);
        let b = bundle_dir.to_string_lossy();
        write_exec(
            &bundle_dir.join("fake-python"),
            &FAKE_PYTHON.replace("__BUNDLE__", &b),
        );
        write_exec(
            &bundle_dir.join("fake-asr"),
            &FAKE_ASR.replace("__BUNDLE__", &b),
        );
        std::fs::write(bundle_dir.join("standin.py"), STANDIN).unwrap();
    }
    let paths = ManagedPaths {
        app_data: base.join("data/app"),
        logs: base.join("data/logs"),
        root: base.join("backend"),
        default_data: base.join("data/asr"),
        default_models: base.join("models"),
        portable: true,
    };
    let sidecar = Sidecar::new(
        &paths.app_data,
        &paths.logs,
        Arc::new(|_: &SidecarStatus| {}),
    );
    Fixture {
        _dir: dir,
        base,
        bundle_dir,
        paths,
        progress: Arc::new(Mutex::new(Vec::new())),
        statuses: Arc::new(Mutex::new(Vec::new())),
        sidecar,
    }
}

impl Fixture {
    fn managed(&self) -> Arc<Managed> {
        let (p, s) = (self.progress.clone(), self.statuses.clone());
        Managed::new(
            self.paths.clone(),
            Bundle::detect(self.bundle_dir.parent().unwrap()),
            self.sidecar.clone(),
            Arc::new(move |x: &ManagedProgress| p.lock().unwrap().push(x.clone())),
            Arc::new(move |x: &ManagedBackendStatus| s.lock().unwrap().push(x.clone())),
        )
    }

    fn mode(&self, m: &str) {
        std::fs::write(self.bundle_dir.join("mode"), m).unwrap();
    }

    fn calls(&self) -> String {
        std::fs::read_to_string(self.bundle_dir.join("calls.log")).unwrap_or_default()
    }

    fn last_progress(&self) -> ManagedProgress {
        self.progress.lock().unwrap().last().cloned().unwrap()
    }
}

fn wait_done(m: &Managed) -> ManagedBackendStatus {
    let t = Instant::now();
    while m.installing.load(Ordering::SeqCst) {
        assert!(
            t.elapsed() < Duration::from_secs(60),
            "setup did not finish"
        );
        std::thread::sleep(Duration::from_millis(50));
    }
    // the worker emits its last status after clearing `installing`
    if let Some(h) = crate::util::lock(&m.worker).take() {
        h.join().unwrap();
    }
    m.status()
}

fn opts(hw: ManagedHardware) -> ManagedInstallOptions {
    ManagedInstallOptions {
        hardware: hw,
        data_dir: None,
        models_dir: None,
    }
}

#[test]
fn external_edition_reports_and_refuses() {
    let f = fixture(false);
    let m = f.managed();
    let st = m.status();
    assert_eq!(st.edition, Edition::External);
    assert_eq!(st.state, ManagedState::NotInstalled);
    assert!(st.data_dir.is_none());
    assert!(m.install(opts(ManagedHardware::Cpu)).is_err());
    assert!(!m.startup());
    assert_eq!(m.detect().len(), 3);
}

#[test]
fn cpu_install_verifies_and_starts_the_sidecar() {
    if !have_python3() {
        eprintln!("no python3; skipped");
        return;
    }
    let f = fixture(true);
    let m = f.managed();
    let st = m.status();
    assert_eq!(st.edition, Edition::Bundled);
    assert_eq!(st.state, ManagedState::NotInstalled);
    assert_eq!(st.backend_version.as_deref(), Some("0.1.0+aaa"));
    assert_eq!(
        st.data_dir.as_deref().map(PathBuf::from),
        Some(f.paths.default_data.clone())
    );

    let st = m.install(opts(ManagedHardware::Cpu)).unwrap();
    assert_eq!(st.state, ManagedState::Installing);
    assert!(
        m.install(opts(ManagedHardware::Cpu)).is_err(),
        "only one at a time"
    );
    let st = wait_done(&m);
    assert_eq!(
        st.state,
        ManagedState::Installed,
        "{st:?} {:?}",
        f.last_progress()
    );
    assert_eq!(st.hardware, Some(ManagedHardware::Cpu));
    assert!(st.env_size_bytes.unwrap() > 0);

    // uv calls: python first, then the locked sync, with everything under the root
    let calls = f.calls();
    let lines: Vec<&str> = calls.lines().collect();
    assert!(
        lines[0].starts_with("python install 3.12 --no-bin --no-registry"),
        "{calls}"
    );
    assert!(
        lines[1].starts_with("sync --frozen --no-dev --extra cpu --extra local"),
        "{calls}"
    );
    let root = f.paths.root.to_string_lossy();
    assert!(
        lines[1].contains(&format!("cache={root}/uv-cache")),
        "{calls}"
    );
    assert!(
        lines[1].contains(&format!("env={root}/env-1/.venv")),
        "{calls}"
    );
    let slot1 = m.active_slot().unwrap();
    assert_eq!(slot1.n, 1);
    assert!(slot1.src().join("server/cli.py").is_file());
    assert!(f.paths.default_data.join("uploads").is_dir());
    assert!(f.paths.default_models.is_dir());

    // progress: phases in order, percent never decreasing, ends with done at 100
    let prog = f.progress.lock().unwrap().clone();
    let phases: Vec<ManagedPhase> = prog.iter().map(|p| p.phase).collect();
    let mut sorted = phases.clone();
    sorted.sort();
    assert_eq!(phases, sorted, "phase order");
    for w in prog.windows(2) {
        assert!(w[1].percent.unwrap() >= w[0].percent.unwrap(), "{w:?}");
    }
    let last = prog.last().unwrap();
    assert_eq!(
        (last.phase, last.percent),
        (ManagedPhase::Done, Some(100.0))
    );
    assert!(prog
        .iter()
        .any(|p| p.log_line.as_deref() == Some("+ torch==2.14.1+cpu")));

    // the sidecar runs the managed backend with the managed environment
    let cfg = f.sidecar.config();
    assert_eq!(PathBuf::from(&cfg.command), slot1.asr_exe());
    assert_eq!(&cfg.args[..4], ["serve", "--host", "127.0.0.1", "--port"]);
    assert_eq!(
        cfg.cwd.as_deref().map(PathBuf::from),
        Some(f.paths.root.clone())
    );
    let sst = f.sidecar.status();
    assert_eq!(sst.state, SidecarState::Running, "{sst:?}");
    std::thread::sleep(Duration::from_millis(300));
    let log = std::fs::read_to_string(f.paths.logs.join("sidecar.log")).unwrap();
    let data = f.paths.default_data.to_string_lossy();
    for want in [
        "ASR_DEVICE=cpu".to_string(),
        "ASR_PROVIDER_DEFAULT=local".to_string(),
        format!("ASR_DATA_DIR={data}"),
        format!("ASR_UPLOAD_ROOTS={data}/uploads"),
    ] {
        assert!(log.contains(&want), "{want} missing in {log}");
    }
    f.sidecar.stop();

    // persisted across a restart; nothing to re-sync, so startup only starts the sidecar
    let m2 = f.managed();
    assert_eq!(m2.status().state, ManagedState::Installed);
    let saved = read_persisted(&f.paths.app_data.join("managed.json"));
    assert_eq!(saved.backend_version.as_deref(), Some("0.1.0+aaa"));
    assert_eq!(saved.env_root.as_deref(), Some(f.paths.root.as_path()));
    let calls_before = f.calls().lines().count();
    assert!(m2.startup(), "managed sidecar is started by startup()");
    let t = Instant::now();
    while f.sidecar.status().state != SidecarState::Running {
        assert!(t.elapsed() < Duration::from_secs(30));
        std::thread::sleep(Duration::from_millis(100));
    }
    assert_eq!(f.calls().lines().count(), calls_before, "no uv call");
    f.sidecar.stop();

    // switching the hardware builds the other slot with the other extra and swaps
    let m3 = f.managed().without_sidecar_start();
    m3.install(opts(ManagedHardware::Rocm)).unwrap();
    let st = wait_done(&m3);
    assert_eq!(st.state, ManagedState::Installed);
    assert_eq!(m3.active_slot().unwrap().n, 2);
    assert!(!slot1.dir.exists(), "old slot removed after the swap");
    assert_eq!(st.hardware, Some(ManagedHardware::Rocm));
    assert!(f
        .calls()
        .lines()
        .last()
        .unwrap()
        .contains("--extra rocm --extra local"));
}

#[test]
fn an_unusable_gpu_fails_clearly() {
    let f = fixture(true);
    f.mode("gpu-fail");
    let m = f.managed().without_sidecar_start();
    m.install(opts(ManagedHardware::Rocm)).unwrap();
    let st = wait_done(&m);
    assert_eq!(st.state, ManagedState::Failed);
    let err = st.error.unwrap();
    assert!(
        err.contains("ROCM") && err.contains("is_available"),
        "{err}"
    );
    assert_eq!(f.last_progress().phase, ManagedPhase::Failed);
    // not marked installed: no sidecar launch registered
    assert!(!read_persisted(&f.paths.app_data.join("managed.json")).installed);
}

#[test]
fn a_uv_failure_is_reported_redacted() {
    let f = fixture(true);
    f.mode("fail");
    let m = f.managed().without_sidecar_start();
    m.install(opts(ManagedHardware::Cpu)).unwrap();
    let st = wait_done(&m);
    assert_eq!(st.state, ManagedState::Failed);
    let err = st.error.unwrap();
    assert!(err.contains("packages failed"), "{err}");
    assert!(
        err.contains("mirror.example") && !err.contains("s3cret"),
        "{err}"
    );
    let all = f.progress.lock().unwrap().clone();
    assert!(all
        .iter()
        .filter_map(|p| p.log_line.as_deref())
        .all(|l| !l.contains("s3cret")));
    let log = std::fs::read_to_string(f.paths.logs.join("managed-backend.log")).unwrap();
    assert!(!log.contains("s3cret"), "{log}");
}

#[test]
fn cancel_kills_the_tool_and_removes_the_partial_env() {
    let f = fixture(true);
    f.mode("slow");
    let m = f.managed().without_sidecar_start();
    m.install(opts(ManagedHardware::Cpu)).unwrap();
    // wait until uv sync is running (the download line arrived)
    let t = Instant::now();
    while !f
        .progress
        .lock()
        .unwrap()
        .iter()
        .any(|p| p.message.contains("torch"))
    {
        assert!(t.elapsed() < Duration::from_secs(20));
        std::thread::sleep(Duration::from_millis(20));
    }
    std::fs::create_dir_all(m.layout().slot(1).venv().join("lib")).unwrap();
    let t = Instant::now();
    let st = m.cancel();
    assert!(
        t.elapsed() < Duration::from_secs(10),
        "cancel took {:?}",
        t.elapsed()
    );
    assert_eq!(st.state, ManagedState::NotInstalled);
    assert!(!m.layout().slot(1).dir.exists(), "partial env removed");
    assert!(f.paths.default_data.is_dir() && f.paths.default_models.is_dir());
    let last = f.last_progress();
    assert_eq!(last.phase, ManagedPhase::Failed);
    assert!(last.message.contains("Cancelled"));
    // a new install can start afterwards
    f.mode("");
    m.install(opts(ManagedHardware::Cpu)).unwrap();
    assert_eq!(wait_done(&m).state, ManagedState::Installed);
}

#[test]
fn startup_resyncs_after_an_update_or_a_move() {
    let f = fixture(true);
    let m = f.managed().without_sidecar_start();
    m.install(opts(ManagedHardware::Cpu)).unwrap();
    assert_eq!(wait_done(&m).state, ManagedState::Installed);
    let syncs = || f.calls().lines().filter(|l| l.starts_with("sync")).count();
    assert_eq!(syncs(), 1);

    // same version, same place: nothing to do (sidecar not configured as managed here)
    let m = f.managed().without_sidecar_start();
    assert!(!m.startup());
    assert!(!m.installing.load(Ordering::SeqCst));

    // app update: another bundled version
    std::fs::write(f.bundle_dir.join("VERSION"), "0.1.0+bbb").unwrap();
    let m = f.managed().without_sidecar_start();
    m.startup();
    assert_eq!(m.status().state, ManagedState::Installing);
    let st = wait_done(&m);
    assert_eq!(st.state, ManagedState::Installed);
    assert_eq!(st.backend_version.as_deref(), Some("0.1.0+bbb"));
    assert_eq!(syncs(), 2);

    // the update was built in the other slot and swapped in
    let active = m.active_slot().unwrap();
    assert_eq!(active.n, 2);
    assert!(!m.layout().slot(1).dir.exists());

    // the portable folder moved: the environment is rebuilt in the other slot
    f.update_env_root(Path::new("/elsewhere/backend"));
    let m = f.managed().without_sidecar_start();
    m.startup();
    assert_eq!(wait_done(&m).state, ManagedState::Installed);
    assert_eq!(m.active_slot().unwrap().n, 1);
    assert!(!active.dir.exists());
    assert_eq!(syncs(), 3);
}

impl Fixture {
    fn update_env_root(&self, root: &Path) {
        let path = self.paths.app_data.join("managed.json");
        let mut p = read_persisted(&path);
        p.env_root = Some(root.to_path_buf());
        std::fs::write(&path, serde_json::to_vec(&p).unwrap()).unwrap();
    }
}

#[test]
fn folder_options_are_checked_and_kept() {
    let f = fixture(true);
    let m = f.managed().without_sidecar_start();
    assert!(m
        .install(ManagedInstallOptions {
            hardware: ManagedHardware::Cpu,
            data_dir: Some("relative/data".into()),
            models_dir: None,
        })
        .unwrap_err()
        .contains("absolute"));
    let models = f.base.join("其他盘 models");
    m.install(ManagedInstallOptions {
        hardware: ManagedHardware::Cpu,
        data_dir: None,
        models_dir: Some(models.to_string_lossy().into()),
    })
    .unwrap();
    let st = wait_done(&m);
    assert_eq!(
        st.models_dir.as_deref().map(PathBuf::from),
        Some(models.clone())
    );
    assert!(models.is_dir());
    // a later install without folders keeps the choice; the launch env uses it
    m.install(opts(ManagedHardware::Cpu)).unwrap();
    let st = wait_done(&m);
    assert_eq!(
        st.models_dir.as_deref().map(PathBuf::from),
        Some(models.clone())
    );
    let launch = f.sidecar.managed_launch().unwrap();
    assert_eq!(launch.command, m.active_slot().unwrap().asr_exe());
    assert!(launch.env.contains(&(
        "ASR_MODEL_CACHE_DIR".into(),
        models.to_string_lossy().into()
    )));

    // portable folder moved: chosen folders under the old root follow it
    let new_base = f.base.with_file_name("moved");
    m.rebase(&f.base, &new_base);
    assert_eq!(
        m.status().models_dir.as_deref().map(PathBuf::from),
        Some(new_base.join("其他盘 models"))
    );
}

#[test]
fn a_failed_or_cancelled_switch_keeps_the_working_environment() {
    let f = fixture(true);
    let m = f.managed().without_sidecar_start();
    m.install(opts(ManagedHardware::Cpu)).unwrap();
    assert_eq!(wait_done(&m).state, ManagedState::Installed);
    let slot1 = m.active_slot().unwrap();

    // GPU check fails: still installed (cpu, slot 1), the error is kept, slot 2 is gone
    f.mode("gpu-fail");
    m.install(opts(ManagedHardware::Rocm)).unwrap();
    let st = wait_done(&m);
    assert_eq!(st.state, ManagedState::Installed, "{st:?}");
    assert_eq!(st.hardware, Some(ManagedHardware::Cpu));
    assert!(st.error.as_deref().unwrap().contains("ROCM"));
    assert_eq!(m.active_slot(), Some(slot1.clone()));
    assert!(slot1.asr_exe().is_file());
    assert!(!m.layout().slot(2).dir.exists());
    assert_eq!(f.last_progress().phase, ManagedPhase::Failed);
    assert_eq!(
        f.sidecar.managed_launch().unwrap().command,
        slot1.asr_exe(),
        "launch still points at the working slot"
    );

    // cancelled switch: same
    f.mode("slow");
    m.install(opts(ManagedHardware::Rocm)).unwrap();
    let t = Instant::now();
    while !m.layout().slot(2).venv().parent().unwrap().exists()
        || !f
            .progress
            .lock()
            .unwrap()
            .iter()
            .rev()
            .take(3)
            .any(|p| p.message.contains("torch"))
    {
        assert!(t.elapsed() < Duration::from_secs(20));
        std::thread::sleep(Duration::from_millis(20));
    }
    assert_eq!(m.status().state, ManagedState::Installing);
    assert_eq!(m.status().hardware, Some(ManagedHardware::Cpu));
    let st = m.cancel();
    assert_eq!(st.state, ManagedState::Installed);
    assert_eq!(st.hardware, Some(ManagedHardware::Cpu));
    assert_eq!(st.error, None, "a cancelled switch clears the error");
    assert!(slot1.asr_exe().is_file());
    assert!(!m.layout().slot(2).dir.exists());

    // a successful switch afterwards clears the error
    f.mode("");
    m.install(opts(ManagedHardware::Rocm)).unwrap();
    let st = wait_done(&m);
    assert_eq!(
        (st.state, st.hardware, st.error),
        (ManagedState::Installed, Some(ManagedHardware::Rocm), None)
    );
}

#[test]
fn status_reports_defaults_and_reveal_dirs() {
    let f = fixture(true);
    let m = f.managed();
    let st = m.status();
    assert_eq!(
        st.default_data_dir.as_deref().map(PathBuf::from),
        Some(f.paths.default_data.clone())
    );
    assert_eq!(
        st.default_models_dir.as_deref().map(PathBuf::from),
        Some(f.paths.default_models.clone())
    );
    assert!(st.portable);
    assert_eq!(st.env_size_bytes, None);
    let d = m.reveal_dir(RevealTarget::Models).unwrap();
    assert_eq!(d, f.paths.default_models);
    assert!(d.is_dir());
    assert_eq!(m.reveal_dir(RevealTarget::Backend).unwrap(), f.paths.root);
    let w: RevealTarget = serde_json::from_value(serde_json::json!("data")).unwrap();
    assert_eq!(w, RevealTarget::Data);
    let ext = fixture(false).managed();
    assert!(ext.reveal_dir(RevealTarget::Data).is_err());
}

#[test]
fn the_proxy_reaches_uv_and_percent_is_rounded() {
    let f = fixture(true);
    let m = f.managed().without_sidecar_start();
    let r = crate::proxy::resolve(
        &crate::ipc::ProxySettings {
            mode: crate::ipc::ProxyMode::Manual,
            url: Some("http://u:p@proxy.example:3128".into()),
            no_proxy: crate::proxy::DEFAULT_NO_PROXY.into(),
        },
        &|_| None,
        None,
    );
    m.set_proxy(Arc::new(move || r.clone()));
    m.install(opts(ManagedHardware::Cpu)).unwrap();
    assert_eq!(wait_done(&m).state, ManagedState::Installed);
    let calls = f.calls();
    for l in calls.lines() {
        assert!(l.contains("proxy=http://u:p@proxy.example:3128"), "{l}");
        assert!(l.contains("192.168.0.0/16"), "{l}");
    }
    // credentials never in the log
    let log = std::fs::read_to_string(f.paths.logs.join("managed-backend.log")).unwrap();
    assert!(
        log.contains("proxy: Manual http://u:***@proxy.example:3128"),
        "{log}"
    );
    assert!(!log.contains("u:p@"), "{log}");
    for p in f.progress.lock().unwrap().iter() {
        let v = p.percent.unwrap();
        assert_eq!((v * 100.0).round() / 100.0, v, "{v}");
    }
}
