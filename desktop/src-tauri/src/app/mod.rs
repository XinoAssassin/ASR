//! Tauri shell: commands / events of `docs/desktop-ipc.md`, tray, global hotkey, hide-on-close.

use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter, Manager, RunEvent, State, WindowEvent, Wry};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};

use crate::config::ConfigStore;
use crate::ipc::{
    AudioProcess, BackendConfig, DeviceList, HardwareSuggestion, ManagedBackendStatus,
    ManagedHardware, ManagedInstallOptions, ManagedPhase, ManagedProgress, ProxySettings,
    ProxyState, ProxyTest, RecorderState, RecorderStatus, Recording, SidecarConfig, SidecarStatus,
    StartOptions,
};
use crate::managed::{env::Bundle, Managed, ManagedPaths, RevealTarget};
use crate::paths::{AppPaths, PerUserDirs};
use crate::proxy::ProxyStore;
use crate::recorder::{capture, Recorder};
use crate::sidecar::Sidecar;
use crate::uploader::{queue::Store, Uploader};

pub const EV_STATUS: &str = "recorder://status";
pub const EV_RECORDINGS: &str = "recordings://changed";
pub const EV_SIDECAR: &str = "sidecar://status";
pub const EV_MANAGED_PROGRESS: &str = "managed-backend://progress";
pub const EV_MANAGED_STATUS: &str = "managed-backend://status";
const HOTKEY: &str = "ctrl+alt+r";

pub struct AppState {
    recorder: Arc<Recorder>,
    config: Arc<ConfigStore>,
    uploader: Arc<Uploader>,
    sidecar: Arc<Sidecar>,
    managed: Arc<Managed>,
    proxy: Arc<ProxyStore>,
    default_out: PathBuf,
    tray_toggle: Mutex<Option<MenuItem<Wry>>>,
}

type Res<T> = Result<T, String>;

fn show_main(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
    }
}

fn emit_status(app: &AppHandle, st: &RecorderStatus) {
    let _ = app.emit(EV_STATUS, st);
}

fn refresh_tray(app: &AppHandle) {
    let state = app.state::<AppState>();
    let recording = state.recorder.state() == RecorderState::Recording;
    if let Some(item) = crate::util::lock(&state.tray_toggle).as_ref() {
        let _ = item.set_text(if recording {
            "停止录音 (Ctrl+Alt+R)"
        } else {
            "开始录音 (Ctrl+Alt+R)"
        });
    }
    if let Some(tray) = app.tray_by_id("main") {
        let _ = tray.set_tooltip(Some(if recording {
            "ASR Recorder — 录音中"
        } else {
            "ASR Recorder"
        }));
    }
}

fn do_start(app: &AppHandle, options: StartOptions) -> Res<RecorderStatus> {
    let state = app.state::<AppState>();
    let out_dir = options
        .out_dir
        .as_ref()
        .filter(|d| !d.trim().is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| state.default_out.clone());
    let st = state.recorder.start(&options, &out_dir)?;
    let partial = state.recorder.active_partial();
    state.config.update_settings(|s| {
        s.last_start = Some(options.clone());
        s.active_partial = partial;
        if !s.out_dirs.contains(&out_dir) {
            s.out_dirs.push(out_dir.clone());
        }
    });
    emit_status(app, &st);
    refresh_tray(app);
    Ok(st)
}

/// Blocking: stops, finalises and queues the recording.
fn do_stop(app: &AppHandle) -> Res<Recording> {
    let state = app.state::<AppState>();
    if state.recorder.state() != RecorderState::Recording {
        return Err("not recording".into());
    }
    let finalizing = {
        let mut s = state.recorder.status(false);
        s.state = RecorderState::Finalizing;
        s
    };
    emit_status(app, &finalizing);
    let res = state.recorder.stop();
    state.config.update_settings(|s| s.active_partial = None);
    emit_status(app, &state.recorder.status(false));
    refresh_tray(app);
    let finished = res?;
    let auto = state.config.get_with_key().auto_upload;
    Ok(state.uploader.add_finished(&finished, auto))
}

fn toggle(app: &AppHandle) {
    let app = app.clone();
    std::thread::spawn(move || {
        let state = app.state::<AppState>();
        let res = match state.recorder.state() {
            RecorderState::Idle => {
                let opts = state.config.settings().last_start.unwrap_or_default();
                do_start(&app, opts).map(|_| ())
            }
            RecorderState::Recording => do_stop(&app).map(|_| ()),
            RecorderState::Finalizing => Ok(()),
        };
        if let Err(e) = res {
            log::error!("toggle recording: {e}");
        }
    });
}

fn quit(app: &AppHandle) {
    let app = app.clone();
    std::thread::spawn(move || {
        let state = app.state::<AppState>();
        if state.recorder.state() == RecorderState::Recording {
            if let Err(e) = do_stop(&app) {
                log::error!("stop on quit: {e}");
            }
        }
        state.sidecar.stop();
        app.exit(0);
    });
}

// ------------------------------------------------------------------------------------ commands
#[tauri::command]
async fn recorder_devices() -> Res<DeviceList> {
    tauri::async_runtime::spawn_blocking(|| {
        capture::init_thread();
        capture::devices()
    })
    .await
    .map_err(|e| e.to_string())?
}

/// Async + `spawn_blocking`: opening the devices can take seconds (bounded by the recorder).
#[tauri::command]
async fn recorder_start(app: AppHandle, options: StartOptions) -> Res<RecorderStatus> {
    tauri::async_runtime::spawn_blocking(move || do_start(&app, options))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn recorder_stop(app: AppHandle) -> Res<Recording> {
    tauri::async_runtime::spawn_blocking(move || do_stop(&app))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
fn recorder_set_options(state: State<'_, AppState>, options: StartOptions) {
    state
        .config
        .update_settings(|s| s.last_start = Some(options));
}

#[tauri::command]
async fn recorder_processes() -> Res<Vec<AudioProcess>> {
    tauri::async_runtime::spawn_blocking(|| {
        capture::init_thread();
        capture::processes()
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
fn sidecar_get_config(state: State<'_, AppState>) -> SidecarConfig {
    state.sidecar.config()
}

#[tauri::command]
fn sidecar_set_config(state: State<'_, AppState>, config: SidecarConfig) -> Res<SidecarConfig> {
    state.sidecar.set_config(config)
}

#[tauri::command]
fn sidecar_status(state: State<'_, AppState>) -> SidecarStatus {
    state.sidecar.status()
}

#[tauri::command]
async fn sidecar_start(app: AppHandle) -> Res<SidecarStatus> {
    tauri::async_runtime::spawn_blocking(move || app.state::<AppState>().sidecar.start())
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
async fn sidecar_stop(app: AppHandle) -> Res<SidecarStatus> {
    tauri::async_runtime::spawn_blocking(move || app.state::<AppState>().sidecar.stop())
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
fn managed_backend_status(state: State<'_, AppState>) -> ManagedBackendStatus {
    state.managed.status()
}

/// Async + `spawn_blocking`: DXGI enumeration loads the graphics stack.
#[tauri::command]
async fn managed_backend_detect(app: AppHandle) -> Res<Vec<HardwareSuggestion>> {
    tauri::async_runtime::spawn_blocking(move || app.state::<AppState>().managed.detect())
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
async fn managed_backend_install(
    app: AppHandle,
    options: ManagedInstallOptions,
) -> Res<ManagedBackendStatus> {
    log::info!("managed backend: install requested: {options:?}");
    tauri::async_runtime::spawn_blocking(move || app.state::<AppState>().managed.install(options))
        .await
        .map_err(|e| e.to_string())?
}

/// Blocks until the setup has stopped and cleaned up.
#[tauri::command]
async fn managed_backend_cancel(app: AppHandle) -> Res<ManagedBackendStatus> {
    tauri::async_runtime::spawn_blocking(move || app.state::<AppState>().managed.cancel())
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
fn network_get_proxy(state: State<'_, AppState>) -> ProxyState {
    state.proxy.state()
}

/// Applies to uploads at once (failed ones are re-armed), the next managed setup and the next
/// sidecar start.
#[tauri::command]
async fn network_set_proxy(app: AppHandle, settings: ProxySettings) -> Res<ProxyState> {
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<AppState>();
        let out = state.proxy.set(settings)?;
        state.uploader.config_changed();
        Ok(out)
    })
    .await
    .map_err(|e| e.to_string())?
}

/// Reach the package index, the Python download host and the model hub (short timeouts).
#[tauri::command]
async fn network_test_proxy(app: AppHandle, settings: Option<ProxySettings>) -> Res<ProxyTest> {
    tauri::async_runtime::spawn_blocking(move || {
        let store = app.state::<AppState>().proxy.clone();
        let r = match settings {
            Some(s) => store.resolve_with(&store.unmask(s)),
            None => store.resolve(),
        };
        crate::proxy::test(&r)
    })
    .await
    .map_err(|e| e.to_string())
}

#[tauri::command]
fn managed_backend_reveal(state: State<'_, AppState>, which: RevealTarget) -> Res<()> {
    let dir = state.managed.reveal_dir(which)?;
    open_folder(&dir)
}

/// Native folder dialog, modal to the main window; null when cancelled.
#[tauri::command]
async fn pick_folder(
    app: AppHandle,
    title: Option<String>,
    default_path: Option<String>,
) -> Res<Option<String>> {
    #[cfg(windows)]
    let owner = app
        .get_webview_window("main")
        .and_then(|w| w.hwnd().ok())
        .map(|h| h.0 as isize);
    #[cfg(not(windows))]
    let owner = {
        let _ = &app;
        None
    };
    tauri::async_runtime::spawn_blocking(move || {
        crate::folder_picker::pick_folder(
            owner,
            title.as_deref(),
            default_path
                .as_deref()
                .filter(|p| !p.trim().is_empty())
                .map(std::path::Path::new),
        )
        .map(|p| p.map(|p| p.to_string_lossy().into_owned()))
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
fn recorder_status(state: State<'_, AppState>) -> RecorderStatus {
    state.recorder.status(false)
}

#[tauri::command]
fn backend_get_config(state: State<'_, AppState>) -> BackendConfig {
    state.config.get_masked()
}

/// Async + `spawn_blocking`: writes the config / key files and re-arms the queue (one save).
#[tauri::command]
async fn backend_set_config(app: AppHandle, config: BackendConfig) -> Res<BackendConfig> {
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<AppState>();
        let out = state.config.set(config)?;
        state.uploader.config_changed();
        Ok(out)
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
fn recordings_list(state: State<'_, AppState>) -> Vec<Recording> {
    state.uploader.store().list()
}

#[tauri::command]
fn recording_upload(state: State<'_, AppState>, id: String) -> Res<Recording> {
    state.uploader.enqueue(&id)
}

#[tauri::command]
fn recording_reveal(state: State<'_, AppState>, id: String) -> Res<()> {
    let e = state
        .uploader
        .store()
        .get(&id)
        .ok_or_else(|| format!("unknown recording {id}"))?;
    let path = PathBuf::from(&e.recording.path);
    reveal(&path)
}

/// `%WINDIR%\explorer.exe` by full path: a bare name would be searched in the (user-writable)
/// app directory first.
#[cfg(windows)]
fn explorer() -> std::process::Command {
    let windir = std::env::var_os("WINDIR")
        .or_else(|| std::env::var_os("SystemRoot"))
        .map(PathBuf::from)
        .filter(|d| d.is_absolute())
        .unwrap_or_else(|| PathBuf::from(r"C:\Windows"));
    std::process::Command::new(windir.join("explorer.exe"))
}

#[cfg(windows)]
fn reveal(path: &std::path::Path) -> Res<()> {
    use std::os::windows::process::CommandExt;
    let mut cmd = explorer();
    if path.exists() {
        // explorer parses its own command line: pass `/select,"<path>"` verbatim.
        cmd.raw_arg(format!("/select,\"{}\"", path.display()));
    } else if let Some(dir) = path.parent() {
        cmd.arg(dir);
    }
    cmd.spawn().map(|_| ()).map_err(|e| e.to_string())
}

/// Open a folder itself in Explorer.
#[cfg(windows)]
fn open_folder(dir: &std::path::Path) -> Res<()> {
    use std::os::windows::process::CommandExt;
    explorer()
        .raw_arg(format!("\"{}\"", dir.display()))
        .spawn()
        .map(|_| ())
        .map_err(|e| e.to_string())
}

#[cfg(not(windows))]
fn open_folder(dir: &std::path::Path) -> Res<()> {
    reveal(&dir.join("."))
}

#[cfg(not(windows))]
fn reveal(path: &std::path::Path) -> Res<()> {
    let dir = path.parent().unwrap_or(path);
    let opener = if cfg!(target_os = "macos") {
        "open"
    } else {
        "xdg-open"
    };
    std::process::Command::new(opener)
        .arg(dir)
        .spawn()
        .map(|_| ())
        .map_err(|e| e.to_string())
}

// ---------------------------------------------------------------------------------------- setup
/// The app's folders: portable (marker next to the exe) or the per-user OS folders.
fn app_paths(app: &tauri::App) -> Result<AppPaths, Box<dyn std::error::Error>> {
    let exe = std::env::current_exe()?;
    let path = app.path();
    let per_user = || -> Result<PerUserDirs, tauri::Error> {
        let app_data = path.app_data_dir()?;
        let app_local_data = path.app_local_data_dir()?;
        Ok(PerUserDirs {
            app_log: path
                .app_log_dir()
                .unwrap_or_else(|_| app_local_data.join("logs")),
            documents: path.document_dir().ok(),
            app_data,
            app_local_data,
        })
    };
    Ok(match crate::paths::portable_root(&exe) {
        Some(root) => AppPaths::portable(&root),
        None => AppPaths::per_user(&per_user()?),
    })
}

/// The main window, created here (not from the config) so a portable build can keep the
/// WebView2 profile (the SPA's local storage) inside its folder.
fn create_main_window(
    app: &tauri::App,
    paths: &AppPaths,
) -> Result<(), Box<dyn std::error::Error>> {
    let cfg = app
        .config()
        .app
        .windows
        .iter()
        .find(|w| w.label == "main")
        .cloned()
        .ok_or("no main window in tauri.conf.json")?;
    let mut b = tauri::WebviewWindowBuilder::from_config(app.handle(), &cfg)?;
    if let Some(dir) = paths.webview.as_ref() {
        std::fs::create_dir_all(dir)?;
        b = b.data_directory(dir.clone());
    }
    b.build()?;
    Ok(())
}

fn setup(app: &mut tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    let handle = app.handle().clone();
    let paths = app_paths(app)?;
    let data_dir = paths.app_data.clone();
    std::fs::create_dir_all(&data_dir)?;
    create_main_window(app, &paths)?;
    let default_out = paths.recordings.clone();
    let config = Arc::new(ConfigStore::open(&data_dir));
    let store = Arc::new(Store::open(&data_dir.join("recordings.json")));
    // A moved portable folder (other drive letter, USB stick): stored paths follow it.
    let moved_from = paths.portable_root.as_ref().and_then(|root| {
        let old = config.settings().portable_root.filter(|o| o != root);
        if let Some(old) = old.as_ref() {
            log::info!(
                "portable folder moved from {} to {}",
                old.display(),
                root.display()
            );
            config.update_settings(|s| s.rebase(old, root));
            store.update_all(|e| {
                e.recording.path = crate::paths::rebase_str(&e.recording.path, old, root);
            });
        }
        config.update_settings(|s| s.portable_root = Some(root.clone()));
        old
    });
    let ev = handle.clone();
    let uploader = Uploader::new(
        store,
        config.clone(),
        Arc::new(move |r: &Recording| {
            let _ = ev.emit(EV_RECORDINGS, r);
        }),
    );
    let log_dir = paths.logs.clone();
    let ev = handle.clone();
    let sidecar = Sidecar::new(
        &data_dir,
        &log_dir,
        Arc::new(move |st: &SidecarStatus| {
            let _ = ev.emit(EV_SIDECAR, st);
        }),
    );
    // One proxy setting for uploads, the sidecar and the managed setup (read at each use).
    let proxy = ProxyStore::open(&data_dir);
    let proxy_fn: crate::proxy::ProxyFn = {
        let p = proxy.clone();
        Arc::new(move || p.resolve())
    };
    {
        let e = proxy.state().effective;
        log::info!(
            "proxy: {:?} {} {:?}",
            e.source,
            e.url.as_deref().unwrap_or("-"),
            e.warnings
        );
    }
    uploader.set_proxy(proxy_fn.clone());
    sidecar.set_proxy(proxy_fn.clone());
    // "Desktop + backend" edition: the backend source and uv ship as resources.
    let bundle = app
        .path()
        .resource_dir()
        .ok()
        .and_then(|d| Bundle::detect(&crate::paths::strip_verbatim(&d)));
    log::info!(
        "edition: {}; portable: {}",
        if bundle.is_some() {
            "bundled"
        } else {
            "external"
        },
        paths.is_portable()
    );
    let (ev, ev2) = (handle.clone(), handle.clone());
    let last_phase = Mutex::new(None::<ManagedPhase>);
    let managed = Managed::new(
        ManagedPaths {
            app_data: data_dir.clone(),
            logs: log_dir.clone(),
            root: paths.managed_root.clone(),
            default_data: paths.managed_data.clone(),
            default_models: paths.models.clone(),
            portable: paths.is_portable(),
        },
        bundle,
        sidecar.clone(),
        Arc::new(move |p: &ManagedProgress| {
            let mut last = crate::util::lock(&last_phase);
            if *last != Some(p.phase) {
                log::info!("managed backend: {:?}: {}", p.phase, p.message);
                *last = Some(p.phase);
            }
            let _ = ev.emit(EV_MANAGED_PROGRESS, p);
        }),
        Arc::new(move |st: &ManagedBackendStatus| {
            log::info!("managed backend status: {:?} {:?}", st.state, st.error);
            let _ = ev2.emit(EV_MANAGED_STATUS, st);
        }),
    );
    managed.set_proxy(proxy_fn);
    if let (Some(old), Some(root)) = (moved_from.as_ref(), paths.portable_root.as_ref()) {
        sidecar.rebase(old, root);
        managed.rebase(old, root);
    }
    // The managed backend starts its sidecar itself (after a re-sync when the bundled backend
    // changed); any other configured sidecar starts here.
    if !managed.startup() && sidecar.config().enabled {
        let sc = sidecar.clone();
        std::thread::spawn(move || {
            let st = sc.start();
            log::info!("sidecar on launch: {:?}", st.state);
        });
    }
    app.manage(AppState {
        recorder: Arc::new(Recorder::new()),
        config: config.clone(),
        uploader: uploader.clone(),
        sidecar,
        managed: managed.clone(),
        proxy,
        default_out: default_out.clone(),
        tray_toggle: Mutex::new(None),
    });

    // Tray.
    let toggle_item =
        MenuItem::with_id(app, "toggle", "开始录音 (Ctrl+Alt+R)", true, None::<&str>)?;
    let show_item = MenuItem::with_id(app, "show", "显示窗口", true, None::<&str>)?;
    let quit_item = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
    let sep = PredefinedMenuItem::separator(app)?;
    let menu = Menu::with_items(app, &[&toggle_item, &show_item, &sep, &quit_item])?;
    let mut tray = TrayIconBuilder::with_id("main")
        .tooltip("ASR Recorder")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "toggle" => toggle(app),
            "show" => show_main(app),
            "quit" => quit(app),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_main(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        tray = tray.icon(icon.clone());
    }
    tray.build(app)?;
    *crate::util::lock(&app.state::<AppState>().tray_toggle) = Some(toggle_item);

    // Global hotkey (a failure, e.g. taken by another app, is not fatal).
    if let Err(e) = app.global_shortcut().register(HOTKEY) {
        log::warn!("global shortcut {HOTKEY} unavailable: {e}");
    }

    // The upload worker starts right away (entries added later wake it), so neither a long
    // recovery nor a panic in it holds back uploads.
    uploader.start();

    // Recover recordings orphaned by a crash. This runs while the hotkey / tray / IPC are
    // live: `Recorder::recover_dir` skips partials owned by a session, so a recording started
    // meanwhile is never finalised under it (no start gate: finalising a long orphan may take
    // minutes).
    {
        let handle = handle.clone();
        let settings = config.settings();
        let mut dirs = settings.out_dirs.clone();
        dirs.push(default_out);
        let crashed_partial = settings.active_partial.clone();
        if let Some(dir) = crashed_partial.as_ref().and_then(|p| p.parent()) {
            dirs.push(dir.to_path_buf());
        }
        dirs.sort();
        dirs.dedup();
        std::thread::spawn(move || {
            let state = handle.state::<AppState>();
            let auto = state.config.get_with_key().auto_upload;
            for dir in dirs {
                for r in state.recorder.recover_dir(&dir) {
                    // Adopted = a finished file from an earlier run; index it only if missing
                    // (an indexed one keeps its state, job and transcript).
                    if r.adopted && state.uploader.has_path(&r.finished.path) {
                        continue;
                    }
                    if r.adopted {
                        log::info!("adopting unindexed {}", r.finished.path.display());
                    }
                    state.uploader.add_finished(&r.finished, auto);
                }
            }
            // Forget the crashed session's partial, unless a recording started meanwhile has
            // already stored its own.
            state.config.update_settings(|s| {
                if s.active_partial == crashed_partial {
                    s.active_partial = state.recorder.active_partial();
                }
            });
        });
    }

    // 10 Hz status events while recording. A session that ended by itself (the file could
    // not be written) is stopped here, so its partial is finalised and queued as usual.
    {
        let handle = handle.clone();
        std::thread::spawn(move || loop {
            std::thread::sleep(Duration::from_millis(100));
            let state = handle.state::<AppState>();
            if state.recorder.state() == RecorderState::Recording {
                emit_status(&handle, &state.recorder.status(true));
                if let Some(reason) = state.recorder.failure() {
                    log::error!("recording ended by itself: {reason}; finalising");
                    if let Err(e) = do_stop(&handle) {
                        log::error!("stop after failure: {e}");
                    }
                }
            }
        });
    }

    // Test hook: record system audio for N seconds right after start (automated end-to-end
    // check of start -> stop -> FLAC -> upload queue inside the real app).
    if let Some(secs) = std::env::var("ASR_RECORDER_AUTO_RECORD")
        .ok()
        .and_then(|v| v.parse::<u64>().ok())
    {
        let handle = handle.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_secs(2));
            let opts = StartOptions {
                mic: std::env::var("ASR_RECORDER_AUTO_RECORD_MIC").is_ok(),
                out_dir: std::env::var("ASR_RECORDER_AUTO_RECORD_DIR").ok(),
                process_id: std::env::var("ASR_RECORDER_AUTO_RECORD_PID")
                    .ok()
                    .and_then(|v| v.parse().ok()),
                ..StartOptions::default()
            };
            match do_start(&handle, opts) {
                Ok(st) => log::info!("auto-record started: {:?}", st.file),
                Err(e) => return log::error!("auto-record start failed: {e}"),
            }
            std::thread::sleep(Duration::from_secs(secs));
            match do_stop(&handle) {
                Ok(r) => log::info!(
                    "auto-record finished: {}",
                    serde_json::to_string(&r).unwrap_or_default()
                ),
                Err(e) => log::error!("auto-record stop failed: {e}"),
            }
        });
    }

    // Test hook: press the tray / hotkey toggle twice, N seconds apart (replays the stored
    // last options, like Ctrl+Alt+R).
    if let Some(secs) = std::env::var("ASR_RECORDER_AUTO_TOGGLE")
        .ok()
        .and_then(|v| v.parse::<u64>().ok())
    {
        let handle = handle.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_secs(2));
            toggle(&handle);
            std::thread::sleep(Duration::from_secs(secs));
            let st = handle.state::<AppState>().recorder.status(false);
            log::info!(
                "auto-toggle status: state {:?} sources {:?} processId {:?} warnings {:?}",
                st.state,
                st.sources,
                st.process_id,
                st.warnings
            );
            toggle(&handle);
        });
    }

    // Test hook: run the managed backend setup for this hardware right after start, and
    // optionally quit N seconds after it finished (automated end-to-end check).
    if let Some(hw) = std::env::var("ASR_RECORDER_AUTO_MANAGED_INSTALL")
        .ok()
        .and_then(|v| serde_json::from_value::<ManagedHardware>(v.trim().into()).ok())
    {
        let managed = managed.clone();
        let handle = handle.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_secs(2));
            let opts = ManagedInstallOptions {
                hardware: hw,
                data_dir: None,
                models_dir: None,
            };
            match managed.install(opts) {
                Ok(_) => log::info!("auto managed install started: {}", hw.as_str()),
                Err(e) => return log::error!("auto managed install: {e}"),
            }
            while managed.status().state == crate::ipc::ManagedState::Installing {
                std::thread::sleep(Duration::from_secs(1));
            }
            let st = managed.status();
            log::info!(
                "auto managed install finished: {}",
                serde_json::to_string(&st).unwrap_or_default()
            );
            if let Some(secs) = std::env::var("ASR_RECORDER_AUTO_EXIT_AFTER_MANAGED")
                .ok()
                .and_then(|v| v.parse::<u64>().ok())
            {
                std::thread::sleep(Duration::from_secs(secs));
                log::info!("ASR_RECORDER_AUTO_EXIT_AFTER_MANAGED reached");
                quit(&handle);
            }
        });
    }

    // Test hook: exit after N seconds (used by the automated launch check).
    if let Some(secs) = std::env::var("ASR_RECORDER_AUTO_EXIT")
        .ok()
        .and_then(|v| v.parse::<u64>().ok())
    {
        let handle = handle.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_secs(secs));
            log::info!("ASR_RECORDER_AUTO_EXIT reached");
            quit(&handle);
        });
    }
    log::info!("ASR Recorder started; data dir {}", data_dir.display());
    Ok(())
}

pub fn run() {
    use tauri_plugin_log::{Target, TargetKind};
    // Portable mode: the log goes to <root>\data\logs instead of %LOCALAPPDATA%.
    let portable = std::env::current_exe()
        .ok()
        .and_then(|exe| crate::paths::portable_root(&exe));
    let mut log = tauri_plugin_log::Builder::new().level(log::LevelFilter::Info);
    if let Some(root) = portable.as_ref() {
        log = log.clear_targets().targets([
            Target::new(TargetKind::Stdout),
            Target::new(TargetKind::Folder {
                path: AppPaths::portable(root).logs,
                file_name: None,
            }),
        ]);
    }
    let mut context = tauri::generate_context!();
    if let Some(root) = portable.as_ref() {
        // Each portable folder is its own app instance (single-instance lock, window class).
        let id = format!(
            "{}.portable-{}",
            context.config().identifier,
            crate::paths::instance_key(root)
        );
        context.config_mut().identifier = id;
    }
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            show_main(app)
        }))
        .plugin(log.build())
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, _shortcut, event| {
                    if event.state == ShortcutState::Pressed {
                        toggle(app);
                    }
                })
                .build(),
        )
        .setup(setup)
        .on_window_event(|window, event| {
            // Closing the window hides it; recording and uploads continue in the tray.
            if let WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == "main" {
                    let _ = window.hide();
                    api.prevent_close();
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            recorder_devices,
            recorder_start,
            recorder_stop,
            recorder_status,
            backend_get_config,
            backend_set_config,
            recordings_list,
            recording_upload,
            recording_reveal,
            recorder_set_options,
            recorder_processes,
            sidecar_get_config,
            sidecar_set_config,
            sidecar_status,
            sidecar_start,
            sidecar_stop,
            managed_backend_status,
            managed_backend_detect,
            managed_backend_install,
            managed_backend_cancel,
            managed_backend_reveal,
            pick_folder,
            network_get_proxy,
            network_set_proxy,
            network_test_proxy,
        ])
        .build(context)
        .expect("error while building the Tauri application");
    app.run(|handle, event| {
        if let RunEvent::ExitRequested { api, code, .. } = &event {
            // Exit requested elsewhere (e.g. session end) while recording: finalise first.
            if code.is_none()
                && handle.state::<AppState>().recorder.state() == RecorderState::Recording
            {
                api.prevent_exit();
                quit(handle);
            }
        }
        if let RunEvent::Exit = &event {
            // Last chance (e.g. exit from elsewhere): never leave the sidecar running.
            handle.state::<AppState>().sidecar.stop();
        }
    });
}
