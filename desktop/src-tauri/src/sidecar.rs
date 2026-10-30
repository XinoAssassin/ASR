//! Optional local backend ("sidecar", PLAN §6.3): the app starts `asr serve` (or any command),
//! waits for `GET http://127.0.0.1:{port}/api/health`, logs its output to the app log dir and
//! stops it with the app. Never two instances: start/stop are serialised and a port that is
//! already in use is reported instead of spawning a second server.

use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crate::ipc::{SidecarConfig, SidecarState, SidecarStatus};

pub const HEALTH_TIMEOUT: Duration = Duration::from_secs(120);
const STOP_GRACE: Duration = Duration::from_secs(10);
const LOG_MAX: u64 = 5 * 1024 * 1024;
const TAIL_LINES: usize = 20;

pub type OnStatus = Arc<dyn Fn(&SidecarStatus) + Send + Sync>;

struct Running {
    child: Child,
    #[cfg(windows)]
    job: Option<win::Job>,
    stopping: bool,
}

pub struct Sidecar {
    config_path: PathBuf,
    log_path: PathBuf,
    config: Mutex<SidecarConfig>,
    status: Mutex<SidecarStatus>,
    running: Mutex<Option<Running>>,
    op: Mutex<()>,
    /// Set by `stop` so a start that is still waiting for health gives up at once.
    abort: std::sync::atomic::AtomicBool,
    tail: Arc<Mutex<Vec<String>>>,
    on_status: OnStatus,
    health_timeout: Duration,
}

/// Rotating append-only log file (one `.1` backup).
struct LogFile {
    path: PathBuf,
}

impl LogFile {
    fn write_line(&self, line: &str) {
        static LOCK: Mutex<()> = Mutex::new(());
        let _g = LOCK.lock().unwrap_or_else(|p| p.into_inner());
        if std::fs::metadata(&self.path).map(|m| m.len()).unwrap_or(0) > LOG_MAX {
            let _ = std::fs::rename(&self.path, self.path.with_extension("log.1"));
        }
        if let Ok(mut f) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
        {
            let _ = writeln!(
                f,
                "{} {line}",
                chrono::Local::now().format("%Y-%m-%d %H:%M:%S")
            );
        }
    }
}

pub fn health_url(port: u16) -> String {
    format!("http://127.0.0.1:{port}/api/health")
}

fn healthy(http: &reqwest::blocking::Client, port: u16) -> bool {
    http.get(health_url(port))
        .send()
        .map(|r| r.status().is_success())
        .unwrap_or(false)
}

/// Origins of the app's own WebView: Windows (`http://tauri.localhost`) and macOS / Linux.
pub const WEBVIEW_ORIGINS: [&str; 2] = ["http://tauri.localhost", "tauri://localhost"];
/// The backend's own default `ASR_CORS_ORIGINS` (Vite dev server, docker nginx).
const BACKEND_DEFAULT_ORIGINS: [&str; 2] = ["http://localhost:5173", "http://localhost:8080"];

/// `ASR_CORS_ORIGINS` for the sidecar: the inherited value (or the backend's defaults when it
/// is unset / empty) plus the WebView origins, so the SPA can call the backend the app started.
pub fn cors_origins(inherited: Option<&str>) -> String {
    let mut list: Vec<String> = inherited
        .unwrap_or("")
        .split(',')
        .map(|o| o.trim().trim_end_matches('/').to_string())
        .filter(|o| !o.is_empty())
        .collect();
    if list.is_empty() {
        list = BACKEND_DEFAULT_ORIGINS.map(String::from).to_vec();
    }
    for o in WEBVIEW_ORIGINS {
        if !list.iter().any(|x| x == o) {
            list.push(o.to_string());
        }
    }
    list.join(",")
}

/// The value of a `--port N` / `--port=N` argument: None if absent, Some(Err) if malformed.
fn port_arg(args: &[String]) -> Option<Result<u16, String>> {
    let i = args
        .iter()
        .position(|a| a == "--port" || a.starts_with("--port="))?;
    let v = match args[i].strip_prefix("--port=") {
        Some(v) => Some(v),
        None => args.get(i + 1).map(String::as_str),
    };
    Some(
        v.and_then(|v| v.trim().parse::<u16>().ok())
            .filter(|p| *p != 0)
            .ok_or_else(|| "args: --port needs a port number".to_string()),
    )
}

/// `args` with the server's port equal to `port`: an existing `--port` value is replaced; for
/// `asr serve` (an argument `serve`) without one, `--port {port}` is appended. Other commands
/// are left alone (they may take the port another way).
pub fn args_with_port(args: &[String], port: u16) -> Vec<String> {
    let mut out = args.to_vec();
    match out
        .iter()
        .position(|a| a == "--port" || a.starts_with("--port="))
    {
        Some(i) if out[i] == "--port" => {
            if i + 1 < out.len() {
                out[i + 1] = port.to_string();
            } else {
                out.push(port.to_string());
            }
        }
        Some(i) => out[i] = format!("--port={port}"),
        None if out.iter().any(|a| a == "serve") => {
            out.push("--port".into());
            out.push(port.to_string());
        }
        None => {}
    }
    out
}

/// Check a config from the SPA. The WebView choosing a program the app runs is the one
/// IPC path to code execution, so `command` must be an absolute path to an existing file
/// (CSP keeps foreign scripts out of the WebView; changes are logged). `args` may not name
/// another port than `port` (the health check would time out and kill a healthy server).
pub fn validate(c: &SidecarConfig) -> Result<SidecarConfig, String> {
    if c.port == 0 {
        return Err("port must be 1..65535".into());
    }
    let command = c.command.trim().to_string();
    if !command.is_empty() {
        let p = Path::new(&command);
        if !p.is_absolute() || !p.is_file() {
            return Err(format!(
                "command must be the full path of an existing program (e.g. \
                 D:\\ASR\\backend\\.venv\\Scripts\\asr.exe): {command}"
            ));
        }
    }
    let cwd = c
        .cwd
        .as_deref()
        .map(str::trim)
        .filter(|d| !d.is_empty())
        .map(str::to_string);
    if let Some(d) = cwd.as_deref() {
        if !Path::new(d).is_dir() {
            return Err(format!("cwd is not a folder: {d}"));
        }
    }
    match port_arg(&c.args) {
        Some(Err(e)) => return Err(e),
        Some(Ok(p)) if p != c.port => {
            return Err(format!(
                "args say --port {p} but the port is {}; make them the same",
                c.port
            ))
        }
        _ => {}
    }
    Ok(SidecarConfig {
        enabled: c.enabled,
        command,
        args: args_with_port(&c.args, c.port),
        cwd,
        port: c.port,
    })
}

fn port_in_use(port: u16) -> bool {
    std::net::TcpStream::connect_timeout(
        &std::net::SocketAddr::from(([127, 0, 0, 1], port)),
        Duration::from_millis(300),
    )
    .is_ok()
}

impl Sidecar {
    /// `data_dir` holds `sidecar.json`; `log_dir` gets `sidecar.log`.
    pub fn new(data_dir: &Path, log_dir: &Path, on_status: OnStatus) -> Arc<Self> {
        let config_path = data_dir.join("sidecar.json");
        let config = std::fs::read(&config_path)
            .ok()
            .and_then(|b| serde_json::from_slice(&b).ok())
            .unwrap_or_default();
        let _ = std::fs::create_dir_all(log_dir);
        Arc::new(Self {
            config_path,
            log_path: log_dir.join("sidecar.log"),
            config: Mutex::new(config),
            status: Mutex::new(SidecarStatus::stopped()),
            running: Mutex::new(None),
            op: Mutex::new(()),
            abort: std::sync::atomic::AtomicBool::new(false),
            tail: Arc::new(Mutex::new(Vec::new())),
            on_status,
            health_timeout: HEALTH_TIMEOUT,
        })
    }

    #[cfg(test)]
    fn with_timeout(mut self: Arc<Self>, t: Duration) -> Arc<Self> {
        Arc::get_mut(&mut self).unwrap().health_timeout = t;
        self
    }

    pub fn config(&self) -> SidecarConfig {
        crate::util::lock(&self.config).clone()
    }

    /// Store a config from the SPA (checked by [`validate`]; `args` get the port, see
    /// [`args_with_port`]). A changed `command` is logged.
    pub fn set_config(&self, c: SidecarConfig) -> Result<SidecarConfig, String> {
        let c = validate(&c)?;
        let old = self.config();
        let body = serde_json::to_vec_pretty(&c).map_err(|e| e.to_string())?;
        crate::util::write_atomic(&self.config_path, &body)
            .map_err(|e| format!("cannot store the sidecar config: {e}"))?;
        if old.command != c.command || old.args != c.args {
            let msg = format!(
                "config changed: command {:?} -> {:?}, args {:?} -> {:?}",
                old.command, c.command, old.args, c.args
            );
            log::warn!("sidecar: {msg}");
            LogFile {
                path: self.log_path.clone(),
            }
            .write_line(&msg);
        }
        *crate::util::lock(&self.config) = c.clone();
        Ok(c)
    }

    pub fn status(&self) -> SidecarStatus {
        crate::util::lock(&self.status).clone()
    }

    fn set_status(&self, st: SidecarStatus) {
        *crate::util::lock(&self.status) = st.clone();
        (self.on_status)(&st);
    }

    fn fail(&self, msg: String) -> SidecarStatus {
        log::warn!("sidecar: {msg}");
        let st = SidecarStatus {
            state: SidecarState::Failed,
            pid: None,
            url: None,
            error: Some(msg),
        };
        self.set_status(st.clone());
        st
    }

    fn last_output(&self) -> String {
        let t = crate::util::lock(&self.tail);
        if t.is_empty() {
            String::new()
        } else {
            format!(
                " — last output: {}",
                t[t.len().saturating_sub(5)..].join(" | ")
            )
        }
    }

    /// Start (blocking until healthy, failed or timed out). Idempotent while running.
    pub fn start(self: &Arc<Self>) -> SidecarStatus {
        let _op = crate::util::lock(&self.op);
        self.abort.store(false, std::sync::atomic::Ordering::SeqCst);
        if crate::util::lock(&self.running).is_some() {
            return self.status();
        }
        let cfg = self.config();
        if cfg.command.trim().is_empty() {
            return self.fail("no sidecar command configured".into());
        }
        if port_in_use(cfg.port) {
            return self.fail(format!(
                "port {} is already in use (another backend running?); not starting a second one",
                cfg.port
            ));
        }
        let http = match reqwest::blocking::Client::builder()
            .no_proxy()
            .timeout(Duration::from_secs(2))
            .build()
        {
            Ok(c) => c,
            Err(e) => return self.fail(format!("cannot create the health-check client: {e}")),
        };
        let log = LogFile {
            path: self.log_path.clone(),
        };
        // A config stored by an older version (or edited by hand) may disagree on the port.
        let args = args_with_port(&cfg.args, cfg.port);
        if args != cfg.args {
            log::warn!(
                "sidecar: args {:?} run as {args:?} (port {})",
                cfg.args,
                cfg.port
            );
        }
        let cors = cors_origins(std::env::var("ASR_CORS_ORIGINS").ok().as_deref());
        log.write_line(&format!(
            "starting: {} {args:?} (ASR_CORS_ORIGINS={cors})",
            cfg.command
        ));
        let mut cmd = Command::new(&cfg.command);
        cmd.args(&args)
            // The SPA in the app's WebView calls this backend directly (CORS + Origin check).
            .env("ASR_CORS_ORIGINS", &cors)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        if let Some(cwd) = cfg.cwd.as_deref().filter(|c| !c.trim().is_empty()) {
            cmd.current_dir(cwd);
        }
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            // No console window; own process group so CTRL_BREAK reaches only the sidecar.
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
            cmd.creation_flags(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP);
        }
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            cmd.process_group(0); // own group: stop() signals the whole tree
        }
        self.set_status(SidecarStatus {
            state: SidecarState::Starting,
            pid: None,
            url: None,
            error: None,
        });
        let mut child = match cmd.spawn() {
            Ok(c) => c,
            Err(e) => return self.fail(format!("cannot start {}: {e}", cfg.command)),
        };
        let pid = child.id();
        crate::util::lock(&self.tail).clear();
        for (name, stream) in [
            (
                "out",
                child
                    .stdout
                    .take()
                    .map(|s| Box::new(s) as Box<dyn std::io::Read + Send>),
            ),
            (
                "err",
                child
                    .stderr
                    .take()
                    .map(|s| Box::new(s) as Box<dyn std::io::Read + Send>),
            ),
        ] {
            let Some(stream) = stream else { continue };
            let log = LogFile {
                path: self.log_path.clone(),
            };
            let tail = self.tail.clone();
            std::thread::spawn(move || {
                for line in BufReader::new(stream).lines().map_while(Result::ok) {
                    log.write_line(&format!("[{name}] {line}"));
                    let mut t = crate::util::lock(&tail);
                    t.push(line);
                    if t.len() > TAIL_LINES {
                        t.remove(0);
                    }
                }
            });
        }
        *crate::util::lock(&self.running) = Some(Running {
            #[cfg(windows)]
            job: win::Job::assign(&child),
            child,
            stopping: false,
        });
        self.set_status(SidecarStatus {
            state: SidecarState::Starting,
            pid: Some(pid),
            url: None,
            error: None,
        });
        let deadline = Instant::now() + self.health_timeout;
        loop {
            if healthy(&http, cfg.port) {
                break;
            }
            let exited = {
                let mut r = crate::util::lock(&self.running);
                match r.as_mut().map(|r| r.child.try_wait()) {
                    Some(Ok(Some(code))) => Some(code.to_string()),
                    Some(Err(e)) => Some(e.to_string()),
                    _ => None,
                }
            };
            if let Some(code) = exited {
                *crate::util::lock(&self.running) = None;
                std::thread::sleep(Duration::from_millis(200)); // let the log threads drain
                return self.fail(format!(
                    "exited during startup ({code}){}",
                    self.last_output()
                ));
            }
            if self.abort.load(std::sync::atomic::Ordering::SeqCst) {
                drop(_op);
                return self.stop();
            }
            if Instant::now() >= deadline {
                drop(_op);
                self.stop();
                return self.fail(format!(
                    "not healthy after {} s{}",
                    self.health_timeout.as_secs(),
                    self.last_output()
                ));
            }
            std::thread::sleep(Duration::from_millis(500));
        }
        log.write_line("healthy");
        let st = SidecarStatus {
            state: SidecarState::Running,
            pid: Some(pid),
            url: Some(format!("http://127.0.0.1:{}", cfg.port)),
            error: None,
        };
        self.set_status(st.clone());
        // Watch for an unexpected exit.
        let me = self.clone();
        std::thread::spawn(move || loop {
            std::thread::sleep(Duration::from_secs(1));
            let mut r = crate::util::lock(&me.running);
            let Some(run) = r.as_mut() else { return };
            if run.child.id() != pid {
                return;
            }
            if let Ok(Some(code)) = run.child.try_wait() {
                let expected = run.stopping;
                *r = None;
                drop(r);
                if !expected {
                    std::thread::sleep(Duration::from_millis(200));
                    me.fail(format!("exited unexpectedly ({code}){}", me.last_output()));
                }
                return;
            }
        });
        st
    }

    /// Stop: graceful signal, then kill the whole process tree after a grace period.
    pub fn stop(&self) -> SidecarStatus {
        self.abort.store(true, std::sync::atomic::Ordering::SeqCst);
        let _op = crate::util::lock(&self.op);
        let pid = {
            let mut r = crate::util::lock(&self.running);
            match r.as_mut() {
                Some(run) => {
                    run.stopping = true;
                    run.child.id()
                }
                None => {
                    let st = SidecarStatus::stopped();
                    if self.status().state != SidecarState::Failed {
                        self.set_status(st.clone());
                    }
                    return self.status();
                }
            }
        };
        LogFile {
            path: self.log_path.clone(),
        }
        .write_line("stopping");
        graceful(pid);
        let deadline = Instant::now() + STOP_GRACE;
        loop {
            let done = {
                let mut r = crate::util::lock(&self.running);
                match r.as_mut() {
                    None => true,
                    Some(run) => matches!(run.child.try_wait(), Ok(Some(_))),
                }
            };
            if done {
                break;
            }
            if Instant::now() >= deadline {
                log::warn!("sidecar did not exit in time; killing the process tree");
                if let Some(run) = crate::util::lock(&self.running).as_mut() {
                    kill_tree(run);
                    let _ = run.child.wait();
                }
                break;
            }
            std::thread::sleep(Duration::from_millis(200));
        }
        // Also take down children that outlived a graceful exit of the parent.
        if let Some(mut run) = crate::util::lock(&self.running).take() {
            kill_tree(&mut run);
        }
        let st = SidecarStatus::stopped();
        self.set_status(st.clone());
        st
    }
}

#[cfg(windows)]
fn graceful(pid: u32) {
    win::ctrl_break(pid);
}

#[cfg(unix)]
fn graceful(pid: u32) {
    // SAFETY: plain kill(2) on our own child's process group.
    unsafe {
        libc::kill(-(pid as i32), libc::SIGTERM);
    }
}

#[cfg(not(any(windows, unix)))]
fn graceful(_pid: u32) {}

fn kill_tree(run: &mut Running) {
    #[cfg(windows)]
    {
        if let Some(job) = run.job.as_ref() {
            job.terminate();
        }
    }
    #[cfg(unix)]
    {
        // SAFETY: plain kill(2) on our own child's process group.
        unsafe {
            libc::kill(-(run.child.id() as i32), libc::SIGKILL);
        }
    }
    let _ = run.child.kill();
}

#[cfg(windows)]
mod win {
    use windows::Win32::Foundation::{CloseHandle, HANDLE};
    use windows::Win32::System::Console::{
        AttachConsole, FreeConsole, GenerateConsoleCtrlEvent, GetConsoleWindow,
        SetConsoleCtrlHandler, CTRL_BREAK_EVENT,
    };
    use windows::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, TerminateJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    /// Job object with KILL_ON_JOB_CLOSE: the sidecar tree dies with the app, even on a crash.
    pub struct Job(HANDLE);

    // SAFETY: a job handle can be used from any thread.
    unsafe impl Send for Job {}

    impl Job {
        pub fn assign(child: &std::process::Child) -> Option<Self> {
            use std::os::windows::io::AsRawHandle;
            // SAFETY: Win32 calls on a fresh job handle and our own child's process handle.
            unsafe {
                let job = CreateJobObjectW(None, None).ok()?;
                let mut info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
                info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                let ok = SetInformationJobObject(
                    job,
                    JobObjectExtendedLimitInformation,
                    &info as *const _ as *const _,
                    std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                )
                .is_ok()
                    && AssignProcessToJobObject(job, HANDLE(child.as_raw_handle())).is_ok();
                if !ok {
                    let _ = CloseHandle(job);
                    log::warn!("sidecar: could not put the process into a job object");
                    return None;
                }
                Some(Self(job))
            }
        }

        pub fn terminate(&self) {
            // SAFETY: valid job handle owned by self.
            unsafe {
                let _ = TerminateJobObject(self.0, 1);
            }
        }
    }

    impl Drop for Job {
        fn drop(&mut self) {
            // SAFETY: closing our own handle (kills the tree if still alive: KILL_ON_JOB_CLOSE).
            unsafe {
                let _ = CloseHandle(self.0);
            }
        }
    }

    /// CTRL_BREAK to the sidecar's console process group (uvicorn shuts down gracefully on
    /// SIGBREAK). Only possible when this process has no console of its own (release GUI app).
    pub fn ctrl_break(pid: u32) {
        // SAFETY: console API calls; we attach to the child's hidden console only briefly.
        unsafe {
            if !GetConsoleWindow().is_invalid() {
                return;
            }
            if AttachConsole(pid).is_err() {
                return;
            }
            let _ = SetConsoleCtrlHandler(None, true);
            let _ = GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid);
            let _ = FreeConsole();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Absolute path of a Python interpreter (`set_config` takes full paths only).
    fn python() -> Option<String> {
        ["python3", "python"].iter().find_map(|p| {
            Command::new(p)
                .args(["-c", "import sys; print(sys.executable)"])
                .output()
                .ok()
                .filter(|o| o.status.success())
                .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
                .filter(|p| Path::new(p).is_file())
        })
    }

    fn free_port() -> u16 {
        std::net::TcpListener::bind("127.0.0.1:0")
            .unwrap()
            .local_addr()
            .unwrap()
            .port()
    }

    const STANDIN: &str = r#"
import http.server, os, sys, time
print("cors=" + os.environ.get("ASR_CORS_ORIGINS", "<unset>"), flush=True)
time.sleep(float(sys.argv[2]))
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        ok = self.path == "/api/health"
        self.send_response(200 if ok else 404); self.end_headers()
        self.wfile.write(b'{"status":"ok"}' if ok else b'')
    def log_message(self, *a): print("req", *a, flush=True)
print("standin listening", flush=True)
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
"#;

    fn sidecar(
        dir: &Path,
        port: u16,
        args: Vec<String>,
    ) -> (Arc<Sidecar>, Arc<Mutex<Vec<SidecarState>>>) {
        let states = Arc::new(Mutex::new(Vec::new()));
        let s2 = states.clone();
        let sc = Sidecar::new(
            dir,
            dir,
            Arc::new(move |st: &SidecarStatus| s2.lock().unwrap().push(st.state)),
        );
        sc.set_config(SidecarConfig {
            enabled: true,
            command: python().unwrap(),
            args,
            cwd: None,
            port,
        })
        .unwrap();
        (sc, states)
    }

    #[test]
    fn starts_until_healthy_logs_and_stops() {
        if python().is_none() {
            eprintln!("no python; skipped");
            return;
        }
        let dir = tempfile::tempdir().unwrap();
        let script = dir.path().join("standin.py");
        std::fs::write(&script, STANDIN).unwrap();
        let port = free_port();
        let (sc, states) = sidecar(
            dir.path(),
            port,
            vec![
                script.to_string_lossy().into(),
                port.to_string(),
                "1.0".into(),
            ],
        );
        let st = sc.start();
        assert_eq!(st.state, SidecarState::Running, "{st:?}");
        assert_eq!(
            st.url.as_deref(),
            Some(format!("http://127.0.0.1:{port}").as_str())
        );
        // second start is a no-op (never two)
        assert_eq!(sc.start().pid, st.pid);
        let st = sc.stop();
        assert_eq!(st.state, SidecarState::Stopped);
        assert!(!port_in_use(port));
        let log = std::fs::read_to_string(dir.path().join("sidecar.log")).unwrap();
        assert!(
            log.contains("standin listening") && log.contains("healthy"),
            "{log}"
        );
        // the child got the WebView origin (whatever the inherited value was)
        let cors = log
            .lines()
            .find_map(|l| l.split("[out] cors=").nth(1))
            .expect("cors line");
        assert!(
            cors.split(',').any(|o| o == "http://tauri.localhost"),
            "{cors}"
        );
        assert_eq!(
            *states.lock().unwrap(),
            vec![
                SidecarState::Starting,
                SidecarState::Starting,
                SidecarState::Running,
                SidecarState::Stopped
            ]
        );
        // config persisted
        let again = Sidecar::new(dir.path(), dir.path(), Arc::new(|_| {}));
        assert_eq!(again.config().port, port);
    }

    #[test]
    fn cors_origins_always_include_the_webview() {
        let all =
            "http://localhost:5173,http://localhost:8080,http://tauri.localhost,tauri://localhost";
        assert_eq!(cors_origins(None), all);
        assert_eq!(cors_origins(Some(" ")), all);
        assert_eq!(
            cors_origins(Some("https://asr.example, http://tauri.localhost/")),
            "https://asr.example,http://tauri.localhost,tauri://localhost"
        );
        assert_eq!(cors_origins(Some(all)), all);
    }

    #[test]
    fn port_args_follow_the_port() {
        let v = |a: &[&str]| a.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        assert_eq!(
            args_with_port(&v(&["serve", "--port", "8000"]), 8001),
            v(&["serve", "--port", "8001"])
        );
        assert_eq!(
            args_with_port(&v(&["serve", "--port=8000", "--x"]), 9),
            v(&["serve", "--port=9", "--x"])
        );
        assert_eq!(
            args_with_port(&v(&["serve"]), 8002),
            v(&["serve", "--port", "8002"])
        );
        assert_eq!(
            args_with_port(&v(&["serve", "--port"]), 7),
            v(&["serve", "--port", "7"])
        );
        // not `asr serve`: left alone
        assert_eq!(
            args_with_port(&v(&["app.py", "8000"]), 8001),
            v(&["app.py", "8000"])
        );
    }

    #[test]
    fn set_config_checks_command_cwd_and_port() {
        let dir = tempfile::tempdir().unwrap();
        let exe = dir.path().join("asr.exe");
        std::fs::write(&exe, b"x").unwrap();
        let sc = Sidecar::new(dir.path(), dir.path(), Arc::new(|_| {}));
        let cfg = |command: &str, args: &[&str], port: u16| SidecarConfig {
            enabled: true,
            command: command.into(),
            args: args.iter().map(|s| s.to_string()).collect(),
            cwd: None,
            port,
        };
        let exe_s = exe.to_string_lossy().to_string();
        // contradictory port
        let e = sc.set_config(cfg(&exe_s, &["serve", "--port", "8000"], 8001));
        assert!(e.unwrap_err().contains("--port 8000"));
        assert!(sc
            .set_config(cfg(&exe_s, &["serve", "--port", "x"], 8000))
            .is_err());
        // not a full path / missing / a folder
        for bad in [
            "asr".to_string(),
            dir.path().join("nope.exe").to_string_lossy().to_string(),
            dir.path().to_string_lossy().to_string(),
        ] {
            let e = sc.set_config(cfg(&bad, &["serve"], 8000)).unwrap_err();
            assert!(e.contains("full path"), "{bad}: {e}");
        }
        let mut c = cfg(&exe_s, &["serve"], 8000);
        c.cwd = Some(dir.path().join("nope").to_string_lossy().into());
        assert!(sc.set_config(c).unwrap_err().contains("cwd"));
        assert_eq!(sc.config(), SidecarConfig::default(), "nothing stored");
        // accepted: the port is added for `asr serve`, empty cwd is null, the change is logged
        let mut c = cfg(&format!(" {exe_s} "), &["serve"], 8001);
        c.cwd = Some(" ".into());
        let got = sc.set_config(c).unwrap();
        assert_eq!(got.command, exe_s);
        assert_eq!(got.args, vec!["serve", "--port", "8001"]);
        assert_eq!(got.cwd, None);
        assert_eq!(sc.config(), got);
        let log = std::fs::read_to_string(dir.path().join("sidecar.log")).unwrap();
        assert!(
            log.contains("config changed") && log.contains("asr.exe"),
            "{log}"
        );
        // an empty command (not configured) is fine
        assert!(sc.set_config(cfg("", &["serve"], 8000)).is_ok());
    }

    #[test]
    fn early_exit_and_timeout_and_port_in_use_fail() {
        if python().is_none() {
            return;
        }
        let dir = tempfile::tempdir().unwrap();
        let port = free_port();
        let (sc, _) = sidecar(
            dir.path(),
            port,
            vec!["-c".into(), "import sys; print('boom'); sys.exit(3)".into()],
        );
        let st = sc.start();
        assert_eq!(st.state, SidecarState::Failed);
        assert!(st.error.as_deref().unwrap().contains("boom"), "{st:?}");

        let (sc, _) = sidecar(
            dir.path(),
            port,
            vec!["-c".into(), "import time; time.sleep(60)".into()],
        );
        let sc = sc.with_timeout(Duration::from_secs(2));
        let t = Instant::now();
        let st = sc.start();
        assert_eq!(st.state, SidecarState::Failed);
        assert!(
            st.error.as_deref().unwrap().contains("not healthy"),
            "{st:?}"
        );
        assert!(t.elapsed() < Duration::from_secs(20));

        let busy = std::net::TcpListener::bind(("127.0.0.1", port)).unwrap();
        let (sc, _) = sidecar(dir.path(), port, vec!["-c".into(), "pass".into()]);
        let st = sc.start();
        assert!(
            st.error.as_deref().unwrap().contains("already in use"),
            "{st:?}"
        );
        drop(busy);
    }
}
