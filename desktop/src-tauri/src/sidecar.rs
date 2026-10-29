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
        self.config.lock().unwrap().clone()
    }

    pub fn set_config(&self, c: SidecarConfig) -> Result<SidecarConfig, String> {
        if c.port == 0 {
            return Err("port must be 1..65535".into());
        }
        let body = serde_json::to_vec_pretty(&c).map_err(|e| e.to_string())?;
        crate::util::write_atomic(&self.config_path, &body)
            .map_err(|e| format!("cannot store the sidecar config: {e}"))?;
        *self.config.lock().unwrap() = c.clone();
        Ok(c)
    }

    pub fn status(&self) -> SidecarStatus {
        self.status.lock().unwrap().clone()
    }

    fn set_status(&self, st: SidecarStatus) {
        *self.status.lock().unwrap() = st.clone();
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
        let t = self.tail.lock().unwrap();
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
        let _op = self.op.lock().unwrap();
        self.abort.store(false, std::sync::atomic::Ordering::SeqCst);
        if self.running.lock().unwrap().is_some() {
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
        let log = LogFile {
            path: self.log_path.clone(),
        };
        log.write_line(&format!("starting: {} {:?}", cfg.command, cfg.args));
        let mut cmd = Command::new(&cfg.command);
        cmd.args(&cfg.args)
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
        self.tail.lock().unwrap().clear();
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
                    let mut t = tail.lock().unwrap();
                    t.push(line);
                    if t.len() > TAIL_LINES {
                        t.remove(0);
                    }
                }
            });
        }
        *self.running.lock().unwrap() = Some(Running {
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
        let http = reqwest::blocking::Client::builder()
            .no_proxy()
            .timeout(Duration::from_secs(2))
            .build()
            .expect("http client");
        let deadline = Instant::now() + self.health_timeout;
        loop {
            if healthy(&http, cfg.port) {
                break;
            }
            let exited = {
                let mut r = self.running.lock().unwrap();
                match r.as_mut().map(|r| r.child.try_wait()) {
                    Some(Ok(Some(code))) => Some(code.to_string()),
                    Some(Err(e)) => Some(e.to_string()),
                    _ => None,
                }
            };
            if let Some(code) = exited {
                *self.running.lock().unwrap() = None;
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
            let mut r = me.running.lock().unwrap();
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
        let _op = self.op.lock();
        let pid = {
            let mut r = self.running.lock().unwrap();
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
                let mut r = self.running.lock().unwrap();
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
                if let Some(run) = self.running.lock().unwrap().as_mut() {
                    kill_tree(run);
                    let _ = run.child.wait();
                }
                break;
            }
            std::thread::sleep(Duration::from_millis(200));
        }
        // Also take down children that outlived a graceful exit of the parent.
        if let Some(mut run) = self.running.lock().unwrap().take() {
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

    fn python() -> Option<String> {
        ["python3", "python"].iter().find_map(|p| {
            Command::new(p)
                .arg("--version")
                .output()
                .ok()
                .filter(|o| o.status.success())
                .map(|_| p.to_string())
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
import http.server, sys, time
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
