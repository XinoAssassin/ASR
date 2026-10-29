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
    BackendConfig, DeviceList, RecorderState, RecorderStatus, Recording, StartOptions,
};
use crate::recorder::{self, capture, Recorder};
use crate::uploader::{queue::Store, Uploader};

pub const EV_STATUS: &str = "recorder://status";
pub const EV_RECORDINGS: &str = "recordings://changed";
const HOTKEY: &str = "ctrl+alt+r";

pub struct AppState {
    recorder: Arc<Recorder>,
    config: Arc<ConfigStore>,
    uploader: Arc<Uploader>,
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
    if let Some(item) = state.tray_toggle.lock().unwrap().as_ref() {
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
        if app.state::<AppState>().recorder.state() == RecorderState::Recording {
            if let Err(e) = do_stop(&app) {
                log::error!("stop on quit: {e}");
            }
        }
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

#[tauri::command]
fn recorder_start(app: AppHandle, options: StartOptions) -> Res<RecorderStatus> {
    do_start(&app, options)
}

#[tauri::command]
async fn recorder_stop(app: AppHandle) -> Res<Recording> {
    tauri::async_runtime::spawn_blocking(move || do_stop(&app))
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

#[tauri::command]
fn backend_set_config(state: State<'_, AppState>, config: BackendConfig) -> Res<BackendConfig> {
    let out = state.config.set(config)?;
    state.uploader.config_changed();
    Ok(out)
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

#[cfg(windows)]
fn reveal(path: &std::path::Path) -> Res<()> {
    use std::os::windows::process::CommandExt;
    let mut cmd = std::process::Command::new("explorer.exe");
    if path.exists() {
        // explorer parses its own command line: pass `/select,"<path>"` verbatim.
        cmd.raw_arg(format!("/select,\"{}\"", path.display()));
    } else if let Some(dir) = path.parent() {
        cmd.arg(dir);
    }
    cmd.spawn().map(|_| ()).map_err(|e| e.to_string())
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
fn setup(app: &mut tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    let handle = app.handle().clone();
    let data_dir = app.path().app_data_dir()?;
    std::fs::create_dir_all(&data_dir)?;
    let default_out = app
        .path()
        .document_dir()
        .unwrap_or_else(|_| data_dir.clone())
        .join("ASR Recordings");
    let config = Arc::new(ConfigStore::open(&data_dir));
    let store = Arc::new(Store::open(&data_dir.join("recordings.json")));
    let ev = handle.clone();
    let uploader = Uploader::new(
        store,
        config.clone(),
        Arc::new(move |r: &Recording| {
            let _ = ev.emit(EV_RECORDINGS, r);
        }),
    );
    app.manage(AppState {
        recorder: Arc::new(Recorder::new()),
        config: config.clone(),
        uploader: uploader.clone(),
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
    *app.state::<AppState>().tray_toggle.lock().unwrap() = Some(toggle_item);

    // Global hotkey (a failure, e.g. taken by another app, is not fatal).
    if let Err(e) = app.global_shortcut().register(HOTKEY) {
        log::warn!("global shortcut {HOTKEY} unavailable: {e}");
    }

    // Recover recordings orphaned by a crash, then start the upload worker.
    {
        let handle = handle.clone();
        let settings = config.settings();
        let mut dirs = settings.out_dirs.clone();
        dirs.push(default_out);
        if let Some(dir) = settings.active_partial.as_ref().and_then(|p| p.parent()) {
            dirs.push(dir.to_path_buf());
        }
        dirs.sort();
        dirs.dedup();
        std::thread::spawn(move || {
            let state = handle.state::<AppState>();
            let auto = state.config.get_with_key().auto_upload;
            for dir in dirs {
                for f in recorder::recover_dir(&dir, None) {
                    state.uploader.add_finished(&f, auto);
                }
            }
            state.config.update_settings(|s| s.active_partial = None);
            state.uploader.start();
        });
    }

    // 10 Hz status events while recording.
    {
        let handle = handle.clone();
        std::thread::spawn(move || loop {
            std::thread::sleep(Duration::from_millis(100));
            let state = handle.state::<AppState>();
            if state.recorder.state() == RecorderState::Recording {
                emit_status(&handle, &state.recorder.status(true));
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
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            show_main(app)
        }))
        .plugin(
            tauri_plugin_log::Builder::new()
                .level(log::LevelFilter::Info)
                .build(),
        )
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
        ])
        .build(tauri::generate_context!())
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
    });
}
