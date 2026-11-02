# ASR Recorder (desktop app)

Tauri 2 shell around the SPA in `../frontend` that records **system audio** ("what you hear",
WASAPI loopback) and the **microphone** into one 2-channel 16 kHz FLAC and uploads it to the
backend, which transcribes it as a `per_channel` job. Windows first (PLAN §6.3, milestones M9 /
M10): per-application capture, an optional local backend sidecar and an NSIS installer; macOS
(ScreenCaptureKit) and Linux (PulseAudio / PipeWire) capture are implemented in the recorder
library (see the status table at the end). The IPC contract with the SPA is frozen in
[`docs/desktop-ipc.md`](../docs/desktop-ipc.md).

## Layout

```
desktop/
  package.json              tauri CLI (pnpm tauri dev / build)
  scripts/make_icons.py     regenerates src-tauri/icons (stdlib only)
  scripts/stage-backend.py  stages the bundled backend + pinned uv.exe into build/managed-backend
  scripts/package-portable.ps1  release zips (both editions), optional NSIS installers
  build/                    git-ignored: staged resources, downloads, dist/ (release zips)
  src-tauri/
    Cargo.toml              feature `app` (default) = Tauri shell; without it a plain library
    tauri.conf.json         frontendDist ../../frontend/dist, devUrl http://localhost:5173
    tauri.bundled.conf.json  "desktop + backend" installer: managed-backend/ resources + NSIS hooks
    nsis/                   uninstall hooks of that installer
    capabilities/           main window: event listen / unlisten only
    src/
      ipc.rs                contract payload types (serde camelCase)
      config.rs             BackendConfig store (key in its own file, masked as "***")
      sidecar.rs            optional local backend: spawn, health check, logs, stop (tree)
      paths.rs              where state lives: portable folder or per-user OS folders
      folder_picker.rs      native folder dialog (pick_folder; Windows IFileOpenDialog)
      managed/              "desktop + backend" edition: managed Python environment
        mod.rs              install / switch / cancel worker, startup re-sync, sidecar wiring
        env.rs              resources, slot layout, uv / backend command lines and env vars
        hardware.rs         DXGI adapter list -> cpu / cuda / rocm suggestions
        progress.rs         uv output -> ManagedProgress (phase, percent, redacted line)
      recorder/
        capture.rs          CaptureSource trait, monotonic clock, devices, processes
        wasapi_capture.rs   WASAPI loopback / capture / process loopback, QPC (Windows)
        sck_capture.rs      ScreenCaptureKit system / app / mic audio (macOS)
        pulse_capture.rs    PulseAudio-protocol monitor + source, pure Rust (Linux)
        resample.rs         band-limited variable-ratio resampler
        timeline.rs         clock map, drift control, silence fill  (the sync logic)
        engine.rs           recorder thread: poll -> timeline -> partial WAV, device re-open
        wav.rs / flac.rs    incremental WAV with patched header; streaming FLAC + verify
        mod.rs              Recorder handle, finalize, crash recovery
      uploader/             persistent queue, chunked upload, job create + poll, backoff
      app/mod.rs            Tauri commands/events, tray, global hotkey, hide-on-close
      bin/selftest.rs       headless acceptance test (records + plays a marker track)
```

## Build and run on Windows (GNU toolchain, no MSVC)

The dev machine has rustup with host `x86_64-pc-windows-gnu` and WinLibs mingw (gcc, windres) on
PATH; no Visual Studio is needed. WebView2 is preinstalled on Windows 10/11.

```powershell
# one-off
cd desktop; pnpm install              # @tauri-apps/cli
# dev: starts the Vite dev server of ../frontend and the Rust shell
pnpm tauri dev
# release build (+ NSIS installer); the SPA is built by beforeBuildCommand
pnpm tauri build
# or without the CLI / installer: build the SPA first, then
cd src-tauri; cargo build --release --features tauri/custom-protocol   # target\release\asr-recorder.exe
```

`tauri/custom-protocol` embeds `frontendDist` and serves it from `http://tauri.localhost` (the
Tauri CLI adds it on `tauri build`). A plain `cargo build --release` produces a dev-mode exe
that loads `devUrl` (`http://localhost:5173`) instead.

From WSL we build in a Windows-side mirror (the source of truth stays in WSL) through
`D:\tmp\asr-toolchain\cargo.ps1`, which puts `%USERPROFILE%\.cargo\bin` on PATH and sets the proxy
and `CARGO_TARGET_DIR`.

Checks: `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`,
`cargo test --no-default-features`. All unit tests live in the library (the `app` shell has
none), and they also build and run on Linux without WebKitGTK. On Windows the lib test exe
*with* the `app` feature cannot start (`STATUS_ENTRYPOINT_NOT_FOUND`): it links Tauri, which
needs common controls v6, but tauri-build embeds the manifest only into binaries.

There is no desktop CI: builds and tests run on the dev machine (GNU toolchain).

Release builds use the GUI subsystem (no console). Logs go to
`%LOCALAPPDATA%\dev.asr.recorder\logs\` (tauri-plugin-log). App data (backend config, API key,
upload queue) lives in `%APPDATA%\dev.asr.recorder\`. Recordings default to
`Documents\ASR Recordings\`.

GNU-toolchain specifics:

* `webview2-com-sys` links `WebView2Loader.dll` dynamically on `*-windows-gnu` (MSVC links it
  statically). The DLL is copied next to the exe in the target dir and must ship with it; a
  missing DLL shows up as exit code `0xC0000135` with no output.
* MinGW's gcc driver links its own `default-manifest.o`, which collided with Tauri's manifest
  (`ld: .rsrc merge failure: multiple non-default manifests`, two `RT_MANIFEST #1` resources).
  `build.rs` shadows that object with an empty one (`-B<OUT_DIR>/no-default-manifest`) and embeds
  one combined manifest (`windows-app-manifest.xml`: common controls v6 + asInvoker + long
  paths). Verified: the exe has exactly one manifest.
* `pnpm tauri build` produces `target/release/bundle/nsis/ASR Recorder_0.1.0_x64-setup.exe`
  (~3.3 MiB; NSIS is downloaded by the bundler); the installer includes `WebView2Loader.dll`.
* `cargo build --no-default-features --features selftest --bin recorder-selftest` builds the
  self-test (it is not part of the app or the installer; no WebView2Loader.dll needed).

Test hooks (environment variables, for automated checks only):

| Variable | Effect |
|---|---|
| `ASR_RECORDER_AUTO_EXIT=<s>` | quit after `s` seconds (finalises a running recording) |
| `ASR_RECORDER_AUTO_RECORD=<s>` | record system audio for `s` seconds right after start |
| `ASR_RECORDER_AUTO_RECORD_DIR=<dir>` | output folder for the auto recording |
| `ASR_RECORDER_AUTO_RECORD_MIC=1` | include the mic in the auto recording |
| `ASR_RECORDER_AUTO_RECORD_PID=<pid>` | auto recording captures only this process tree |
| `ASR_RECORDER_AUTO_TOGGLE=<s>` | press the tray / hotkey toggle, and again `s` seconds later |
| `ASR_RECORDER_TEST_MIC_SKEW_PPM=<n>` | pretend the mic clock is off by `n` ppm (drift-controller test) |
| `ASR_RECORDER_AUTO_MANAGED_INSTALL=cpu\|cuda\|rocm` | run `managed_backend_install` 2 s after start (bundled edition) |
| `ASR_RECORDER_AUTO_EXIT_AFTER_MANAGED=<s>` | with the above: quit `s` seconds after the setup finished |

## Editions, portable releases and the managed backend

### Two editions, one exe

| Edition | Contents | `managed_backend_status().edition` |
|---|---|---|
| Recorder | `asr-recorder.exe` (+ `WebView2Loader.dll`) | `external`: connects to a backend you run elsewhere |
| Desktop + backend | the same exe + `managed-backend\` (backend source, `uv.exe`, `VERSION`) | `bundled`: sets up and runs a local backend itself |

The edition is detected at runtime: `managed-backend\` must hold `uv.exe`, `source\pyproject.toml`
and `source\uv.lock` in Tauri's resource dir, which on Windows is the exe's folder (also for an
unpacked, not installed exe). The resource is staged by `scripts/stage-backend.py`: `backend/asr`,
`backend/server`, `pyproject.toml`, `uv.lock`, `.python-version` (no tests, data, models, `.venv`,
caches) into `build/managed-backend/source/`, plus `uv.exe` of **uv 0.13.1**
(`x86_64-pc-windows-msvc`, from the astral-sh/uv GitHub release; SHA-256
`3b2661a4…80a672`, checked against both the value pinned in the script and the release's
`.sha256` file) and `VERSION` = `<project version>+<sha256 of the staged source, 12 hex>`.
The repository has no licence file; `README.txt` in the resource names uv's licence (MIT / Apache-2.0).

### Releases are portable zips

```powershell
# Windows: rustup/cargo, pnpm and Python 3.11+ on PATH (proxy variables are honoured)
powershell -ExecutionPolicy Bypass -File desktop\scripts\package-portable.ps1   # [-SkipFrontend] [-SkipBuild] [-Installer]
```

builds the SPA, the release exe (`cargo build --release --features tauri/custom-protocol`),
stages the backend and writes to `desktop\build\dist\`:

| File | Size (0.1.0) | Contents |
|---|---|---|
| `ASR-Recorder-<v>-win-x64-portable.zip` | 4.3 MB | exe, `WebView2Loader.dll`, `portable`, `README.txt` |
| `ASR-Recorder-<v>-win-x64-with-backend-portable.zip` | 18.8 MB | the same + `managed-backend\` (33 MB unpacked, 32 MB of it uv.exe) |

Each zip holds one top-level folder. `-Installer` additionally builds the two NSIS installers
(`pnpm tauri build`, and `pnpm tauri build --config src-tauri/tauri.bundled.conf.json` renamed to
`…_x64_with-backend-setup.exe`); releases do not use them.

**WebView2**: Windows 11 includes it. Windows 10 without it needs the Evergreen Runtime from
<https://developer.microsoft.com/microsoft-edge/webview2/> (a zip cannot run the bootstrapper);
the zip's `README.txt` says so.

### Portable mode

A file `portable` (or `portable.txt`) next to the exe puts **all** state under that folder
(`src/paths.rs`, one resolution used by the config store, upload queue, sidecar, managed backend,
log plugin and WebView):

```
<root>\asr-recorder.exe, WebView2Loader.dll, portable, README.txt, managed-backend\
<root>\data\app\       backend.json, api-key, settings.json, recordings.json, sidecar.json, managed.json
<root>\data\logs\      ASR Recorder.log, sidecar.log, managed-backend.log
<root>\data\webview\   WebView2 profile (the SPA's local storage: backend profiles, UI settings)
<root>\data\asr\       ASR_DATA_DIR of the managed backend (asr.db, uploads\, config.toml)
<root>\recordings\      default recordings folder
<root>\models\          ASR_MODEL_CACHE_DIR (+ .cache\ for the hub libraries / MIOpen); can be moved in the wizard
<root>\backend\         managed environment: env-1\ / env-2\ (src\ + .venv\), uv-cache\, python\
```

Without the marker the per-user folders are used as before (`%APPDATA%\dev.asr.recorder`,
`%LOCALAPPDATA%\dev.asr.recorder\logs`, `Documents\ASR Recordings`; managed backend in
`%LOCALAPPDATA%\dev.asr.recorder\{backend,data,models}`).

* Each portable folder is its own app instance: the identifier becomes
  `dev.asr.recorder.portable-<hash of the folder>` (single-instance lock, window class), so a
  portable copy runs next to an installed one. Both register Ctrl+Alt+R; the second one logs that
  the shortcut is taken.
* Moving the folder (another drive letter, a USB stick) is supported: the last root is stored in
  `settings.json`; at the next start paths under the old root (recordings index, output folders,
  sidecar command / cwd, chosen data / models folders) are rebased, and the managed environment
  (venvs hold absolute paths) is rebuilt in the other slot from the local uv cache.
* Removing the app = quit it and delete the folder. **That also deletes the recordings, the
  backend database / uploads and the models stored inside it** (the wizard can put models
  elsewhere; recordings can go to any folder). The zip's `README.txt` warns about this.

### Managed backend (desktop + backend edition)

First run (the SPA's setup wizard calls the commands of `docs/desktop-ipc.md`):

1. `managed_backend_detect`: display adapters via DXGI. NVIDIA → `cuda` (CUDA 12.6 build; RTX 50 /
   Blackwell is reported unavailable: no sm_100/120 kernels). AMD gfx1151 (Strix Halo, Radeon
   8060S / 8050S / 8040S, PCI 1002:1586) → `rocm`; other AMD GPUs are unavailable with the reason
   (the Windows ROCm build has gfx1151 kernels only; e.g. Radeon 890M is gfx1150). Else `cpu`.
   One option is `recommended` (CUDA, then ROCm, then CPU); `downloadBytes` ≈ 1 / 2.5 / 5 GB.
2. `managed_backend_install { hardware, dataDir?, modelsDir? }` runs in a background thread
   (no console windows, proxy variables inherited, every step logged to `managed-backend.log`):
   * `prepare`: folders; the target slot is wiped and gets a copy of the bundled source;
   * `python`: `uv python install 3.12 --no-bin --no-registry` into `<backend>\python`
     (`UV_PYTHON_INSTALL_DIR`; nothing in `~\.local\bin` or the registry);
   * `packages`: `uv sync --frozen --no-dev --extra <hw> --extra local --managed-python` with
     `UV_PROJECT_ENVIRONMENT=<slot>\.venv`, `UV_CACHE_DIR=<backend>\uv-cache`;
   * `finalize`: `<slot>\.venv\Scripts\python.exe` imports torch / transformers / funasr /
     `server.cli`; for `cuda` / `rocm` it requires the matching torch build,
     `torch.cuda.is_available()` and a small kernel on the GPU, else the setup **fails** with a
     clear message (no silent CPU fallback).
   Progress: `managed-backend://progress` (phase, bilingual message, percent weighted by
   download sizes and rounded to two decimals, redacted uv line: credentials / query strings
   stripped).
3. On success the slot becomes active, the app writes the sidecar config (enabled,
   `<slot>\.venv\Scripts\asr.exe serve --host 127.0.0.1 --port <free port>`, cwd = backend
   root) and starts it, then reports `installed`. Inside Rust (not in the public `SidecarConfig`)
   the managed sidecar gets `ASR_DATA_DIR`, `ASR_UPLOAD_ROOTS=<data>\uploads`,
   `ASR_MODEL_CACHE_DIR`, `ASR_DEVICE=<hw>`, `ASR_PROVIDER_DEFAULT=local`, `ASR_API_KEY=` (open
   loopback mode), `MODELSCOPE_CACHE` / `HF_HOME` / `MIOPEN_*` under `<models>\.cache`,
   `PYTHONUTF8=1`, plus `ASR_CORS_ORIGINS` as for any sidecar.
4. Models are then downloaded from the SPA (Settings → local models).

Two slots make re-installs safe: switching hardware (or a re-sync) builds the inactive slot while
the current backend keeps running (`state: installing`), stops it only to swap, then deletes the
old slot. A cancelled switch returns to `installed` (`error: null`), a failed one to `installed`
with `error` set; the partial slot is removed either way, data and models are never touched.
A cancelled / failed first install ends `not_installed` / `failed`.

At app start (bundled edition, installed): if the bundled `VERSION` differs from the installed one
(app update), the root moved, or the active venv is incomplete, the environment is re-synced
(`installing`, fast from the cache when only our source changed) before the sidecar starts;
otherwise the managed sidecar starts directly (on its stored port, or a new free one if that is
taken). A sidecar the user pointed elsewhere (`sidecar_set_config`) is left alone.

`managed_backend_reveal { which: data | models | backend }` opens the folder in Explorer
(`%WINDIR%\explorer.exe` by full path); `pick_folder { title?, defaultPath? }` shows the shell's
folder dialog (IFileOpenDialog, modal to the main window) and returns the path or null — no
dialog / fs plugin is exposed to the WebView.

Disk usage: ≈1.5 GB for CPU (venv 1.3 GB, uv-managed Python 68 MB, cache ≈0.1 GB beyond the
hard-linked wheels), ROCm ≈5 GB more; models come on top (several GB). uv hard-links wheels from
its cache into the venvs (same volume); during a switch two environments exist until the swap.

### End-to-end check (2026-11-02, this PC: Ryzen AI MAX+ 392 / Radeon 8060S, Windows 11)

Portable "with backend" zip unpacked to `D:\tmp\asr-bundled\portable-test\ASR 录音 测试\`
(spaces + CJK), launched with `ASR_RECORDER_AUTO_MANAGED_INSTALL=cpu`:

| Step | Result |
|---|---|
| detect | rocm recommended (`AMD Radeon(TM) 8060S Graphics (ROCm, gfx1151)`, DXGI 1002:1586), cuda unavailable |
| cpu setup | python 21 s, packages 6 min 07 s (109 packages, downloads through the proxy), verify 14 s (torch 2.14.1+cpu); 410 s in total |
| sidecar | `env-1\.venv\Scripts\asr.exe serve --host 127.0.0.1 --port 58422`; `/api/health` ok, `/api/system/hardware` default cpu; `managed_backend_status` = installed, `envSizeBytes` 1.35 GB |
| relaunch | managed sidecar running 2 s after start, no uv call |
| folder moved (`ASR moved 移动`) | paths rebased, environment rebuilt in `env-2` from the cache in 31 s, old slot removed, same port |
| switch to rocm | old CPU backend kept answering during the setup (`installing`); the run was cancelled after 19 min while `rocm-sdk-core` was still downloading → back to `installed` (cpu), `error: null`, partial `env-1` removed |
| exit | no process left from the folder (job object) |
| per-user folders | nothing new or changed under `%APPDATA%` / `%LOCALAPPDATA%\dev.asr.recorder*` from these runs |

Not verified here: a completed ROCm (or CUDA) setup and its GPU check, the NSIS installers at
run time (both build; the uninstall hook is untested), macOS / Linux shells.

### Proxy (`src/proxy.rs`)

One setting (`proxy.json` in the app data dir, portable-aware): `system` (default), `none` or
`manual` (`http://`, `https://`, `socks5://`, credentials allowed) plus a no-proxy list (default
`localhost,127.0.0.1,::1,10.*,172.16.*-172.31.*,192.168.*,*.local`).

* `system`: an `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY` variable of the app process wins
  (`source: env`, its `NO_PROXY` is added); else the Windows settings
  (`HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings`: `ProxyEnable`,
  `ProxyServer` — `host:port` or `http=…;https=…;socks=…`, https first — and `ProxyOverride`,
  `<local>` = names without a dot). A PAC script (`AutoConfigURL`) is reported in `pacUrl` with
  a warning (not supported: choose manual).
* The effective proxy is re-read at each use and goes to `uv` (every managed setup step) and to
  every sidecar start as `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` (+ lowercase; removed for `none`)
  and `NO_PROXY` in the conventional form (`10.0.0.0/8`, `172.16.0.0/12`, `.local`, …); the
  uploader uses it for non-loopback backends with the no-proxy list (wildcards, ranges, CIDR,
  `<local>`), loopback always direct; SOCKS is left to uv / Python (the uploader goes direct).
* Credentials: stored in `proxy.json` (0600 on Unix), returned and logged only as
  `user:***@host`; sending that masked URL back keeps the stored password (also for
  `network_test_proxy` drafts), any other URL replaces it.
* `network_test_proxy` fetches PyPI, GitHub (python-build-standalone) and Hugging Face through the
  setting (5 s connect / 10 s total, in parallel) and reports each with its latency.
* Verified on the dev PC (registry: `127.0.0.1:1081` with a LAN override list, no env variables in
  the app): `system` → `http://127.0.0.1:1081`, all three hosts reachable through it; a manual URL
  with credentials came back masked and the masked draft tested with the stored password.

**Installed (NSIS) edition, uninstall**: the hook `nsis/managed-backend-hooks.nsh` deletes
`%LOCALAPPDATA%\dev.asr.recorder\backend` (venvs, uv cache, Python) but never `data\` or
`models\`; with "delete app data" ticked, Tauri removes the whole `%LOCALAPPDATA%` folder, so the
hook moves `data\` and `models\` aside first and back afterwards. Updates (`/UPDATE`) keep
everything; the next start re-syncs.

## How recording works

* **Capture.** Shared-mode WASAPI, polled every ~10 ms with a 1 s device buffer: loopback of the
  default (or chosen) render endpoint and the default (or chosen) capture endpoint, each in its
  mix format (float32 / int16/24/32, any rate). Every packet carries the QPC timestamp WASAPI
  reports for its first frame. Each source is downmixed to mono.
* **Timeline / drift.** Track 0 (system, or the mic when it records alone) is the *reference*:
  it is resampled at the fixed nominal ratio, so its own sample clock defines the file's time
  axis, and a clock map `QPC -> output frame` is anchored to it (smoothed anchor + a rate
  estimate from 20 s+ of continuous data). The mic is the *follower*: a PI controller
  (τ ≈ 2 s, ±1 % max) slews its resampling ratio so every packet lands where the map puts its
  timestamp, i.e. the mic is resampled onto the loopback clock. Offsets above 40 ms are
  re-placed at once (silence insert / drop) instead of slewed.
* **Silence fill.** WASAPI loopback delivers nothing while nothing plays. Each tick, any track
  that lags `map(now) - 250 ms` is filled with silence; when packets resume they are placed at
  `map(timestamp)`, so an idle gap keeps its real duration and both channels stay aligned.
* **Resampler.** Kaiser-windowed sinc (16 zero crossings, cutoff 0.9 × 8 kHz) with absolute
  input/output positions, so the landing position of any input sample is exact; the ratio may
  change per sample.
* **Files.** `<stem>.partial.wav` (2 ch when both sources are on, else mono) is appended
  continuously; the header is patched every second and the data synced every 10 s. On stop the
  WAV is encoded to `<stem>.flac.part` (flacenc, frame by frame, constant memory), decoded again
  and compared sample by sample, renamed to `<stem>.flac`, and only then is the partial removed.
  If FLAC fails the WAV is kept as `<stem>.wav`. After a crash, orphaned `*.partial.wav` files in
  the known output folders are repaired (sizes from the file length) and finalised at the next
  start, and finished `<stem>.flac` / `.wav` files from earlier runs that are missing from the
  queue (a crash between finalising and queueing) are adopted. A crash between renaming the FLAC
  into place and removing the partial leaves both: finalising then verifies the existing FLAC
  against the partial and, if it holds every frame, only removes the partial (one recording, one
  queue entry). Recovery runs in the background while the tray / hotkey / IPC are live (the
  upload worker starts first, so a long recovery never holds back uploads); `Recorder::recover_dir`
  skips partials owned by a session, so recording can start at once and is never finalised under
  itself, and a panic on one leftover file is logged and does not end recovery.
* **Write failure / dead recorder thread.** If the engine cannot write the file for too long it
  ends the session by itself; if the recorder thread dies (a panic in a capture backend) the
  WAV writer flushes on unwind. Either way `Recorder::failure()` reports it and the app's status
  loop stops the recording, so the partial is finalised and queued as usual (the write-failure
  warning is in `RecorderStatus.warnings`; the thread death is logged).
* **Sources that cannot be opened** do not fail the start: with mic + system requested and no
  microphone, the recording runs system-only (mono) with a bilingual warning; only when no
  source opens does `recorder_start` fail. `RecorderStatus.sources` reports what is captured.
* **Device loss.** A capture error does not stop the recording: the track is silence-filled, a
  warning is shown, and the device is reopened every 2 s (rate changes are handled).
* **Default-device changes** are followed: for sources recording "the default device", the
  default endpoint id is checked every 2 s and the source reopens on the new device (the track
  continues at the new device's rate; the switch is placed by timestamps like any gap). If the
  new device cannot be opened, the track shows the "device disconnected" warning and is retried
  every 2 s.
* **Per-application capture** (`StartOptions.processId`, Windows 10 2004+): WASAPI process
  loopback (`AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS`, include the process tree) via the `wasapi`
  crate, 48 kHz float stereo, event-mode client polled like the others. The process-loopback
  stream is produced continuously by the audio engine (silence while the app is quiet) with
  QPC timestamps that match its sample count, so it is a regular reference track; the clock
  map's rate estimate is a least-squares fit, so one badly stamped packet cannot skew it. A
  `processId` that no longer exists, or process loopback that cannot be opened (Windows older
  than 10 2004, policy), falls back to the whole device with a warning (`processId` in the
  status is then `null`); an app that
  exits mid-recording yields a warning (and silence). `recorder_processes` lists processes with
  a non-expired audio session on any render endpoint (active first) with exe name and main
  window title.
* **Warnings** (`RecorderStatus.warnings`, bilingual, shown verbatim by the SPA): a source that
  could not be opened, a source silent for ≥ 60 s, a disconnected device, a vanished target
  process, a write error.
* **driftMs** is the follower's filtered residual offset against the map (ms) — what is left
  after resampling; it stays well below 1 ms in steady state.

## Uploads

`recordings.json` in the app data dir is the queue and the recordings index. The worker thread:
`POST /api/uploads/init` → `PUT /api/uploads/{session}/parts/{n}` (8 MiB parts, progress
persisted per part, so an interrupted upload resumes at the next part even after a restart) →
`POST /api/uploads/{session}/complete` → `POST /api/jobs` → polls `GET /api/jobs/{id}` (5 s,
then 15 s, then 60 s) until `succeeded` (`done`, `transcriptId` set) or `failed`/`cancelled`.

Job body: `options = jobOptions` with `preprocess.channels` forced to `per_channel` for 2-channel
files when the SPA left it unset or `auto` (explicit `downmix`/`left`/`right` are kept), and
`per_channel` replaced by `auto` for mono files; `provider`, `term_set_ids`, `extra_terms` from the
config. `jobOptions: null` means upload only.

Errors: network / 5xx / 408 / 429 → state `failed` with `nextRetryAt` (exponential backoff 10 s
→ 30 min, ±20 % jitter, unlimited attempts); other 4xx → `failed` without `nextRetryAt` until the
user retries (`recording_upload`); any `backend_set_config` re-arms failed uploads / job
creations immediately (the SPA pushes its backend URL and key at every start; one pass over the
queue, one save). While a job is transcribing, an unreachable backend only delays the next poll,
and the local file is no longer needed (polling continues if it was moved). Error texts are
redacted (no query strings / keys). Recordings are never deleted.

* **Resume.** The upload session stores the file's size and modification time; an interrupted
  upload resumes at the next part only if both match, otherwise a new session starts.
* **Timeouts.** 300 s per request; a part upload gets at least the time its body needs at
  64 kbit/s plus a minute (8 MiB: ~18 min), so a slow uplink does not fail the same part forever.
* **File gone.** The recording is needed only until it is on the server: once `uploadId` is set,
  job creation and polling work without the local file; a missing file before that (or when the
  backend lost the upload and it must be sent again) is a permanent failure.
* **Complete.** The backend's `complete` is idempotent, so a lost response is retried with the
  same session and returns the same upload; 409 (a concurrent duplicate still assembling) is
  transient. 404 (session expired) or another 4xx drops the session: the next attempt uploads
  afresh instead of replaying a failing `complete`.
* **404 from `POST /api/jobs`** means "upload not found" or "term set '<id>' not found". The
  worker asks `GET /api/uploads/{id}`: gone → upload again; still there → a permanent job
  failure with a bilingual message (a deleted term set: fix the term sets, then retry; no
  re-upload loop).
* **Redirects are not followed** (reqwest would forward `X-API-Key` to the redirect target): a
  3xx is a permanent error naming the target, so the user sets the final URL.
* **Index.** An entry is never replaced: a file that is already indexed (e.g. found again by
  recovery) keeps its state, job and transcript; a different file whose stem is taken (same
  second, another folder) is indexed as `<stem>_2`.

## Local backend sidecar

`SidecarConfig` (`sidecar.json` in the app data dir): `command`, `args` (default
`serve --port 8000`), `cwd`, `port`, `enabled`. When enabled the app starts it at launch;
`sidecar_start` / `sidecar_stop` do it on demand; `sidecar://status` reports every change.

* `sidecar_set_config` accepts only a `command` that is the absolute path of an existing file
  (e.g. `D:\ASR\backend\.venv\Scripts\asr.exe`; empty = not configured) and a `cwd` that is an
  existing folder; command / args changes are logged (app log and `sidecar.log`).
* `port` and `args` must agree: a `--port N` / `--port=N` argument with another value than
  `port` is rejected; `asr serve` args without `--port` get `--port {port}` appended (the stored
  and returned config shows it). At start the same rule is applied to configs stored by older
  versions (a mismatching `--port` is replaced by `port`).
* `ASR_CORS_ORIGINS` for the sidecar = the app's inherited value (or the backend's defaults
  `http://localhost:5173,http://localhost:8080` when unset) plus the WebView origins
  `http://tauri.localhost` (Windows) and `tauri://localhost` (macOS / Linux), so the SPA's own
  API calls to the backend the app started pass CORS and the Origin check. The variable is set in
  the child's environment, so it takes precedence over a `backend/.env` value: put extra origins
  into the app's environment instead. The sidecar binds `127.0.0.1` (no API key needed).

* Spawned without a console window (`CREATE_NO_WINDOW`) in its own process group and in a Job
  object with `KILL_ON_JOB_CLOSE`, so the whole process tree dies with the app even if the app
  crashes. stdout/stderr go to `<app log dir>\sidecar.log` (rotated at 5 MB, one `.1` backup).
* Startup: `GET http://127.0.0.1:{port}/api/health` every 0.5 s until 200 (`running`, `url`
  set), early exit (`failed` with the exit code and the last output lines) or 120 s (`failed`,
  process tree killed).
* Never two: start/stop are serialised, a second start while running is a no-op, and a port
  that is already in use (e.g. a backend started by hand) is reported as `failed` instead of
  spawning another server.
* Stop (also on quit): `CTRL_BREAK` to the sidecar's process group (uvicorn shuts down
  gracefully on SIGBREAK; possible because the release app has no console of its own), 10 s
  grace, then the Job object is terminated (kills the tree). On Linux / macOS: SIGTERM to the
  process group, then SIGKILL.

## Security

* The WebView may only use the event API (`capabilities/default.json`: `core:event:allow-listen`
  / `allow-unlisten`; the app's own commands need no permission). No window / tray / menu / path
  / shell / fs APIs are exposed to the SPA.
* `managed_backend_install` runs only the bundled `uv.exe` and the venv it created; the WebView
  chooses the hardware and folders, never a program.
* `sidecar_set_config` + `sidecar_start` let the WebView choose a program the app runs: the one
  IPC path to code execution. It is limited by the CSP (only the bundled scripts run in the
  WebView), by accepting only an absolute path to an existing file, and by logging every
  command change; a stored config is never sent anywhere.
* Windows Explorer (`recording_reveal`) is started by full path (`%WINDIR%\explorer.exe`), not
  searched for in the (user-writable) app directory.
* The API key is stored in `api-key` in the app data dir (never in `backend.json`), returned to
  the SPA only as `"***"`, and sent only as `X-API-Key` to the configured backend (no redirects).
  On Unix the file is created with mode 0600 (older 0644 files are tightened at start); on
  Windows the per-user `%APPDATA%` ACL applies.
* Content Security Policy (`tauri.conf.json`, sent as a header for the embedded SPA):
  `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'
  data: blob:; media-src 'self' blob:; connect-src 'self' ipc: http://ipc.localhost
  http://127.0.0.1:* http://localhost:* https: http:; object-src 'none'; base-uri 'self';
  form-action 'none'; frame-src 'none'; frame-ancestors 'none'`.
  * Scripts: only the bundled ones (Tauri adds hashes for its own init scripts); no inline /
    `eval` / remote scripts.
  * `style-src 'unsafe-inline'`: Naive UI (css-render) injects `<style>` elements at runtime.
    `dangerousDisableAssetCspModification: ["style-src"]` keeps Tauri from adding style hashes
    / nonces, which would make browsers ignore `'unsafe-inline'`.
  * `connect-src` allows any http(s) origin because the backend URL is user-configurable
    (LAN hosts, a remote https server); `ipc:` / `http://ipc.localhost` is Tauri's IPC; the
    loopback entries are covered by `http:` but listed for clarity. Blob URLs (waveform audio,
    downloads) are media / navigation, not connections.
  * Verified on Windows (release build, CDP): the SPA renders with its runtime styles, IPC calls
    succeed, a remote `<script>` is blocked, and the SPA itself logs no CSP violation.

## Tray, hotkey, window

* Tray icon: start/stop recording, show window, quit (quit finalises a running recording and
  stops the sidecar first). Left click shows the window.
* `Ctrl+Alt+R` toggles recording with the last stored start options (`recorder_start` and
  `recorder_set_options` store them; default: both sources, default devices, default folder).
  A missing mic only produces a warning, so the toggle works on mic-less machines. If the
  shortcut is taken by another app, the app still starts (logged).
* Closing the window hides it to the tray; recording and uploads continue.
* Single instance: starting the app again focuses the running one.

## Acceptance self-test

`recorder-selftest.exe` (console binary, Windows) records with the real recorder while it plays
a chirp marker every 2 s through the default render device via WASAPI, releases the render
stream for an idle gap in the middle, then cross-correlates every marker in channel 0 of the
FLAC against the QPC time the marker was played (from `IAudioClock`), and reports offsets /
drift for both halves and across the gap. The mic channel is checked for presence and, if the
speakers are audible to the mic, for acoustic marker pickup. Optionally it uploads the FLAC and
creates a job:

```powershell
recorder-selftest.exe --minutes 5 --gap 20 --out D:\tmp\asr-desktop\rec `
  --backend http://127.0.0.1:8090 --target-dir selftest --asr-model seaco-paraformer
```

Results on the dev machine (2026-10-29, over RDP: the only endpoint is the virtual "Remote
Audio" device and there is no microphone, so the second track was a second loopback stream of
the same device via `--mic-device`, with `ASR_RECORDER_TEST_MIC_SKEW_PPM` injecting a clock
error that the controller must remove):

| Run | Ch 0 vs. play time: start / end / drift | Across 20 s idle gap | Ch 1 − ch 0: start / end |
|---|---|---|---|
| 5 min, mic skew +300 ppm | +0.00 / −0.01 / −0.01 ms | −0.01 ms | −0.26 / 0.00 ms (90 ms uncorrected) |
| 30 min, mic skew −250 ppm | +0.00 / −0.01 / −0.01 ms | −0.01 ms | +0.21 / 0.00 ms (450 ms uncorrected) |

All 890 markers of the 30-min run were found; the FLAC (2 ch, 16 kHz, 1803.1 s, 2.8 MB) was
uploaded and the job was created with `preprocess.channels = "per_channel"`.

**Per-application capture** (`--process-test --seconds 40 --gap-at 20 --gap 8`): the self-test
starts two child players, one playing the chirp track (with a stream release in the middle), one
a continuous 1 kHz tone at −22 dBFS, and records (1) with `processId` = the chirp player, then
(2) the whole device; it reports the markers found and the residual / 1 kHz level of channel 0
outside the markers. `--probe-process` prints the raw packet timing of process loopback.

| Run (2026-10-30) | Chirps found | 1 kHz tone of the other process | Notes |
|---|---|---|---|
| process loopback, chirp player only | 16 / 16 | absent: −200 dBFS (digital silence), residual RMS −200 dBFS | drift within each half 0.00 ms |
| whole device | not measured | — | the RDP audio endpoint disappeared (session disconnected) just as this run started |

Process loopback delivers packets continuously (silence while the app is quiet) with timestamps
that match the sample count exactly (probe: 30 s, 0.00 ms discontinuity). Across the player's
stream restart the markers moved by −30 ms against the device play position: process loopback
taps the app's stream when the engine mixes it, while the reference time comes from the render
device clock (`IAudioClock`), whose latency differs per render stream — a property of the
reference, not of the recording (no silence was inserted or dropped).

## Installer (Windows)

`pnpm tauri build` → `target/release/bundle/nsis/ASR Recorder_0.1.0_x64-setup.exe`:
per-user install (`installMode: currentUser`, no admin rights), Simplified Chinese / English
installer UI chosen from the system language, Start-menu folder "ASR Recorder", publisher /
description metadata in "Apps & features". The uninstaller removes the program files, the
Start-menu entry and the uninstall registry key; it never touches recordings (they live in
`Documents\ASR Recordings` or the chosen folder) and keeps the app data (queue, config) unless
the user ticks "delete app data". Silent mode: `setup.exe /S /D=<dir>`, `uninstall.exe /S`.

**SmartScreen (unsigned build).** The installer and exe are not code-signed, so Windows shows
"Windows protected your PC" on first run: click **More info → Run anyway** (or right-click the
file → Properties → tick **Unblock** before running). Signing with an Authenticode certificate
(`bundle.windows.certificateThumbprint` / `signCommand`) removes this once the tool leaves the
team.

## Platform status

| Platform | Capture | Built / tested here |
|---|---|---|
| Windows 10/11 | WASAPI device loopback + mic; process loopback (2004+) | yes: unit tests, self-tests, app + installer |
| macOS 13+ | ScreenCaptureKit: display audio or one app (`processId`); mic through SCK on macOS 15+ (older: system only + warning); needs Screen Recording / Microphone permission | recorder library `cargo check`/`clippy --target aarch64-apple-darwin --no-default-features` clean from Windows; the Tauri shell needs Apple's toolchain (`objc2-exception-helper` compiles Objective-C); **never run on a Mac** |
| Linux | PulseAudio native protocol (pure Rust, works with PipeWire-pulse): monitor of the default sink + default source | unit tests on WSL; a real 2-channel capture through WSLg's PulseAudio (RDP sink / source) recorded a 4.0 s FLAC; per-process capture not supported |

## Known limitations

* Device-wide loopback records every sound on the output device, including notifications; use
  per-application capture to avoid that.
* Exclusive-mode / DRM-protected audio is silent in loopback (the 60 s silence warning shows).
* Unsigned binaries: SmartScreen warns on first start (see above).
* Upload progress within a file is not reported (only state changes).
* macOS: the Tauri shell has not been built (needs a Mac); `devices()` returns pseudo devices
  (SCK has no endpoint list); `processes()` lists all shareable apps, not only those with audio.
* Linux: no per-process capture; desktop shell untested (needs WebKitGTK).
