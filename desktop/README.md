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
  src-tauri/
    Cargo.toml              feature `app` (default) = Tauri shell; without it a plain library
    tauri.conf.json         frontendDist ../../frontend/dist, devUrl http://localhost:5173
    capabilities/           core:default for the main window
    src/
      ipc.rs                contract payload types (serde camelCase)
      config.rs             BackendConfig store (key in its own file, masked as "***")
      sidecar.rs            optional local backend: spawn, health check, logs, stop (tree)
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
  queue (a crash between finalising and queueing) are adopted. Recovery runs in the background
  while the tray / hotkey / IPC are live; `Recorder::recover_dir` skips partials owned by a
  session, so recording can start at once and is never finalised under itself.
* **Write failure.** If the engine cannot write the file for too long it ends the session by
  itself; the app's status loop then stops the recording, so the partial is finalised and
  queued as usual (the warning is in `RecorderStatus.warnings`).
* **Sources that cannot be opened** do not fail the start: with mic + system requested and no
  microphone, the recording runs system-only (mono) with a bilingual warning; only when no
  source opens does `recorder_start` fail. `RecorderStatus.sources` reports what is captured.
* **Device loss.** A capture error does not stop the recording: the track is silence-filled, a
  warning is shown, and the device is reopened every 2 s (rate changes are handled).
* **Default-device changes** are followed: for sources recording "the default device", the
  default endpoint id is checked every 2 s and the source reopens on the new device (the track
  continues; the switch is placed by timestamps like any gap).
* **Per-application capture** (`StartOptions.processId`, Windows 10 2004+): WASAPI process
  loopback (`AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS`, include the process tree) via the `wasapi`
  crate, 48 kHz float stereo, event-mode client polled like the others. The process-loopback
  stream is produced continuously by the audio engine (silence while the app is quiet) with
  QPC timestamps that match its sample count, so it is a regular reference track; the clock
  map's rate estimate is a least-squares fit, so one badly stamped packet cannot skew it. A
  `processId` that no longer exists falls back to the whole device with a warning; an app that
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
