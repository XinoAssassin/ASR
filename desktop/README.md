# ASR Recorder (desktop app)

Tauri 2 shell around the SPA in `../frontend` that records **system audio** ("what you hear",
WASAPI loopback) and the **microphone** into one 2-channel 16 kHz FLAC and uploads it to the
backend, which transcribes it as a `per_channel` job. Windows first (PLAN §6.3, milestone M9).
The IPC contract with the SPA is frozen in [`docs/desktop-ipc.md`](../docs/desktop-ipc.md).

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
      recorder/
        capture.rs          CaptureSource trait, monotonic clock, device list
        wasapi_capture.rs   WASAPI loopback / capture, QPC timestamps (Windows)
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
cd src-tauri; cargo build --release   # target\release\asr-recorder.exe
```

From WSL we build in a Windows-side mirror (the source of truth stays in WSL) through
`D:\tmp\asr-toolchain\cargo.ps1`, which puts `%USERPROFILE%\.cargo\bin` on PATH and sets the proxy
and `CARGO_TARGET_DIR`.

Checks: `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`, `cargo test`. The
platform-independent logic (resampler, timeline, WAV/FLAC, queue, uploader against a mock
backend) also builds and tests on Linux without WebKitGTK:
`cargo test --no-default-features`.

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
* `cargo build --no-default-features --bin recorder-selftest` builds the self-test without Tauri
  (no WebView2Loader.dll needed).

Test hooks (environment variables, for automated checks only):

| Variable | Effect |
|---|---|
| `ASR_RECORDER_AUTO_EXIT=<s>` | quit after `s` seconds (finalises a running recording) |
| `ASR_RECORDER_AUTO_RECORD=<s>` | record system audio for `s` seconds right after start |
| `ASR_RECORDER_AUTO_RECORD_DIR=<dir>` | output folder for the auto recording |
| `ASR_RECORDER_AUTO_RECORD_MIC=1` | include the mic in the auto recording |
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
  start.
* **Device loss.** A capture error does not stop the recording: the track is silence-filled, a
  warning is shown, and the device is reopened every 2 s (rate changes are handled).
* **Warnings** (`RecorderStatus.warnings`, shown verbatim by the SPA): a source silent for
  ≥ 60 s, a disconnected device, a write error.
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
creations immediately (the SPA pushes its backend URL and key at every start); 404 on an upload session or
upload restarts that stage. While a job is transcribing, an unreachable backend only delays the
next poll. Error texts are redacted (no query strings / keys). Recordings are never deleted.

## Tray, hotkey, window

* Tray icon: start/stop recording, show window, quit (quit finalises a running recording first).
  Left click shows the window.
* `Ctrl+Alt+R` toggles recording with the last used start options (both sources, default
  devices, default folder if never started from the UI). If the shortcut is taken by another
  app, the app still starts (logged).
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

## Known limitations (M9)

* Windows only; macOS (ScreenCaptureKit) and per-process loopback are M10.
* Device-wide loopback records every sound on the output device, including notifications.
* The default device is resolved at start; switching the Windows default output mid-recording
  is not followed (a removed device is reopened, by id, when it comes back).
* Exclusive-mode / DRM-protected audio is silent in loopback (the 60 s silence warning shows).
* Unsigned binaries: SmartScreen warns on first start.
* Starting fails (with an error) when an enabled source cannot be opened, e.g. no microphone;
  the tray / hotkey toggle reuses the last options, so on a machine without a mic it only works
  after a start with the mic switched off.
* Upload progress within a file is not reported (only state changes).
