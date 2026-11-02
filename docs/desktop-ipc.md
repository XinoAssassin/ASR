# Desktop app: Tauri IPC contract

Frozen interface between the Rust core (`desktop/src-tauri/`) and the SPA (`frontend/src/platform/`).
Change it only through the lead; both sides code against this file. PLAN §6.3 has the product view.

All payloads are JSON (serde `camelCase` on the Rust side). Times are seconds (float), levels are
linear peak amplitudes 0..1, sizes are bytes.

## Who does what

- **Rust** captures audio, writes the recording incrementally, keeps a persistent upload queue and
  uploads finished recordings to the backend itself (chunked upload API, then `POST /api/jobs`),
  so uploads continue and retry even when no window is open. It never deletes a recording (AGENTS
  rule 8): the file stays in the output folder after a successful job.
- **SPA** shows the recorder UI, passes the backend connection and default job options to Rust,
  and lists recordings and their upload state. Web builds never import `@tauri-apps/*`; everything
  goes through `src/platform/` with `isDesktop()`.

## Types

```ts
type Device = { id: string; name: string; isDefault: boolean }

type RecorderState = 'idle' | 'recording' | 'finalizing'

type RecorderStatus = {
  state: RecorderState
  elapsed: number              // seconds since start (0 when idle)
  levels: { system: number; mic: number }   // peak since the last event
  file: string | null          // path of the recording in progress / last finished one
  driftMs: number | null       // measured mic-vs-loopback drift, after resampling
  warnings: string[]           // e.g. "system audio silent for 60 s"
  sources: { system: boolean; mic: boolean }  // M10: what is actually captured (a missing mic
                               // falls back to system-only with a warning instead of failing)
  processId: number | null     // M10: process tree being captured, null = whole device
}

type StartOptions = {
  system: boolean              // capture the default (or chosen) render device in loopback
  mic: boolean
  outputDevice: string | null  // Device.id from listDevices().outputs; null = default
  inputDevice: string | null
  outDir: string | null        // null = <Documents>/ASR Recordings
  processId?: number | null    // M10: capture only this process tree (Windows 10 2004+ process
                               // loopback); null / absent = the whole render device
}

type AudioProcess = { pid: number; name: string; title: string | null }   // M10

type SidecarConfig = {         // M10: optional local backend started by the app
  enabled: boolean             // start with the app, stop on quit
  command: string              // e.g. D:\ASR\backend\.venv\Scripts\asr.exe
  args: string[]               // default ["serve", "--port", "8000"]
  cwd: string | null
  port: number                 // health checked at http://127.0.0.1:{port}/api/health
}

type SidecarStatus = {
  state: 'stopped' | 'starting' | 'running' | 'failed'
  pid: number | null
  url: string | null           // http://127.0.0.1:{port} while running
  error: string | null
}

type UploadState = 'pending' | 'uploading' | 'uploaded' | 'transcribing' | 'done' | 'failed'

type Recording = {
  id: string                   // stable, e.g. file stem
  path: string
  name: string
  size: number
  duration: number
  createdAt: string            // ISO 8601
  upload: {
    state: UploadState
    attempts: number
    uploadId: string | null
    jobId: string | null
    transcriptId: string | null  // set once the job succeeded
    error: string | null       // last error, redacted (no keys / signed URLs)
    nextRetryAt: string | null
  }
}

type BackendConfig = {
  baseUrl: string              // e.g. http://127.0.0.1:8000 (no trailing slash)
  apiKey: string | null        // sent as X-API-Key; stored by Rust in the app data dir, never logged
  targetDir: string            // upload target_dir on the server
  autoUpload: boolean
  jobOptions: object | null    // body.options for POST /api/jobs (JobOptions); null = no job
  provider: 'local' | 'cloud' | null
  termSetIds: string[]         // body.term_set_ids for POST /api/jobs
  extraTerms: object[]         // body.extra_terms (Term objects)
}
type ManagedHardware = 'cpu' | 'cuda' | 'rocm'

type ManagedBackendStatus = {          // "desktop + backend" edition only
  edition: 'bundled' | 'external'      // external = plain recorder build (no bundled backend)
  state: 'not_installed' | 'installing' | 'installed' | 'failed'
  hardware: ManagedHardware | null     // what the environment was built for
  backendVersion: string | null        // bundled backend source version / hash
  dataDir: string | null               // ASR_DATA_DIR of the managed backend (asr.db, uploads)
  modelsDir: string | null             // ASR_MODEL_CACHE_DIR (large; may live on another drive)
  envSizeBytes: number | null          // Python environment size on disk
  defaultDataDir: string | null        // where dataDir goes when not chosen (portable: inside the app folder)
  defaultModelsDir: string | null
  portable: boolean                    // app state lives next to the exe (portable marker present)
  error: string | null                 // last failure, redacted
}

type HardwareSuggestion = {
  id: ManagedHardware
  available: boolean                   // a matching GPU / build exists for this PC
  recommended: boolean
  label: string                        // e.g. "AMD Radeon 8060S (ROCm, gfx1151)"
  detail: string | null                // why not available
  downloadBytes: number | null         // approximate one-time download for this choice
}

type ManagedInstallOptions = {
  hardware: ManagedHardware
  dataDir?: string | null              // null = default under %LOCALAPPDATA%
  modelsDir?: string | null
}

type ManagedProgress = {
  phase: 'prepare' | 'python' | 'packages' | 'finalize' | 'done' | 'failed'
  message: string                      // bilingual short status
  percent: number | null               // overall 0..100 when known
  logLine: string | null               // raw installer output line (redacted), for a log view
}
```

## Commands (`invoke(name, args)`)

| Command | Args | Returns | Notes |
|---|---|---|---|
| `recorder_devices` | — | `{ outputs: Device[]; inputs: Device[] }` | |
| `recorder_start` | `{ options: StartOptions }` | `RecorderStatus` | Error if already recording or both sources off |
| `recorder_stop` | — | `Recording` | Finalises the FLAC; queues the upload when `autoUpload` |
| `recorder_status` | — | `RecorderStatus` | |
| `backend_get_config` | — | `BackendConfig` (with `apiKey` masked as `"***"` when set) | |
| `backend_set_config` | `{ config: BackendConfig }` | `BackendConfig` (masked) | `apiKey: "***"` keeps the stored key |
| `recordings_list` | — | `Recording[]` | Newest first |
| `recording_upload` | `{ id: string }` | `Recording` | Queue now (manual or retry) |
| `recording_reveal` | `{ id: string }` | — | Open the folder in Explorer / Finder |
| `recorder_set_options` | `{ options: StartOptions }` | — | M10: store the options the tray / Ctrl+Alt+R toggle replays, without starting (the SPA calls it, debounced, when options change) |
| `recorder_processes` | — | `AudioProcess[]` | M10: processes with an active audio session, for `processId` |
| `sidecar_get_config` | — | `SidecarConfig` | M10 |
| `sidecar_set_config` | `{ config: SidecarConfig }` | `SidecarConfig` | M10; does not start / stop by itself. Rejects a `command` that is not an absolute path to an existing file, a `cwd` that is not a folder, and a `--port` in `args` that contradicts `port`; may append `--port {port}` for `asr serve` (the returned config shows it) |
| `sidecar_status` | — | `SidecarStatus` | M10 |
| `sidecar_start` | — | `SidecarStatus` | M10; resolves once healthy or failed (timeout 120 s) |
| `sidecar_stop` | — | `SidecarStatus` | M10 |
| `managed_backend_status` | — | `ManagedBackendStatus` | Bundled edition; `edition: 'external'` and `state: 'not_installed'` in the plain build |
| `managed_backend_detect` | — | `HardwareSuggestion[]` | cpu / cuda / rocm with availability and one `recommended` |
| `managed_backend_install` | `{ options: ManagedInstallOptions }` | `ManagedBackendStatus` | Starts (or re-runs, e.g. to switch hardware) the environment setup in the background; progress via `managed-backend://progress`; error if already installing |
| `managed_backend_cancel` | — | `ManagedBackendStatus` | Stops a running setup (partial environment is removed; data and models are kept). When it was a re-install / hardware switch, the previous working environment is kept and stays `installed` (`error: null`); a *failed* switch also stays `installed`, with `error` set. During a switch the old sidecar keeps running and `state` is `installing` |
| `managed_backend_reveal` | `{ which: 'data' \| 'models' \| 'backend' }` | — | Opens that folder in Explorer |
| `pick_folder` | `{ title?: string, defaultPath?: string \| null }` | `string \| null` | Native folder picker (desktop only); null when cancelled |

## Events (`listen(name)`)

| Event | Payload | Rate |
|---|---|---|
| `recorder://status` | `RecorderStatus` | ~10 Hz while recording, once on each state change |
| `recordings://changed` | `Recording` | On every upload state change |
| `sidecar://status` | `SidecarStatus` | M10: on every state change |
| `managed-backend://progress` | `ManagedProgress` | During setup; a final `done` / `failed` |
| `managed-backend://status` | `ManagedBackendStatus` | On every state change |

## Recording file

Track layout (PLAN §4.5 `per_channel`): channel 0 = system audio (downmixed to mono), channel 1 =
microphone. With only one source enabled the file is mono. 16 kHz, 16-bit FLAC, named
`YYYY-MM-DD_HH-MM-SS.flac`. While recording, audio goes to `<name>.partial.wav` in the output folder
(written incrementally, header patched periodically so a crash leaves a playable file); stop
converts it to FLAC and only then removes the `.partial.wav`.

Jobs for 2-channel recordings use `options.preprocess.channels = "per_channel"` unless the SPA's
`jobOptions` say otherwise. For a **mono** recording Rust replaces `per_channel` with `auto` (the
backend rejects `per_channel` on mono files). For a 2-channel recording a missing or `auto`
channels option becomes `per_channel` (the backend's `auto` may downmix correlated stereo);
explicit `downmix` / `left` / `right` are kept.

Recorder warnings (bilingual, shown verbatim in `RecorderStatus.warnings`) include: a source that
could not be opened (recording continues without it), "per-app capture unavailable, recording the
whole output device" (`processId` is then null), "write failed, retrying (N s held in memory)",
"recording stopped: …" after 300 s of failed writes (the core then finalises and queues what was
written), and "recording paused for about N s (system sleep?), gap skipped" when no track delivered
audio for more than 10 s (`elapsed` excludes the cut time).

Startup recovery runs in the background and never blocks `recorder_start`: orphaned partials are
finalised and queued, and finished `<stem>.flac` / `.wav` files missing from the queue are adopted.

Tray / hotkey toggle: replays the last stored `StartOptions` (`recorder_start` and
`recorder_set_options` both store them). A stored `processId` whose process is gone falls back to
the whole device with a warning.

Upload state: a transient failure is `failed` with `nextRetryAt` set (it retries by itself); a
permanent one (4xx other than 408 / 429) has `nextRetryAt: null` and waits for `recording_upload`
or a config change. Warning strings are bilingual and shown verbatim.

## Backend notes

- The connection pushed by `backend_set_config` is the SPA's *active backend profile* (built-in
  本机 or a remote one); switching profiles pushes the new URL / key, and the SPA keeps the
  recording upload settings per profile.
- Tauri 2 on Windows serves the SPA from `http://tauri.localhost` (`tauri://localhost` on macOS /
  Linux); a backend must list that origin in `ASR_CORS_ORIGINS` for the SPA's own API calls. The
  app-started sidecar gets it added automatically; only a backend started by hand needs it set. Rust's uploads are not browser
  requests and need no CORS.
- With `ASR_API_KEY` empty the backend accepts only loopback Host names, so a remote backend needs
  a key.

## "Desktop + backend" edition

A second installer ships the same app plus the backend source (`backend/asr`, `backend/server`,
`pyproject.toml`, `uv.lock`) and `uv.exe` as Tauri resources. The Python environment is created
on first run (D75):

- `managed_backend_install` runs `uv` with the locked dependencies (`--frozen --no-dev`, extras
  `<hardware>` + `local`) into a per-user environment under `%LOCALAPPDATA%`, using a uv-managed
  Python 3.12. Needs internet once (≈1 GB CPU, ≈2.5 GB CUDA, ≈5 GB ROCm).
- A re-install or hardware switch builds the new environment next to the old one and swaps it in only on success, so a cancelled or failed switch keeps the previous working backend.
- On success the app writes the sidecar config itself (enabled, `asr serve` on 127.0.0.1, a free
  port) and starts it, and only then emits `installed` (a failed start is reported through
  `sidecar://status`); the SPA's 本机 profile follows the sidecar URL. The managed backend listens
  on loopback only, with no API key (open mode).
- The sidecar gets `ASR_DATA_DIR`, `ASR_UPLOAD_ROOTS`, `ASR_MODEL_CACHE_DIR`, `ASR_DEVICE` and
  `ASR_PROVIDER_DEFAULT=local` from the managed install, plus the CORS origins.
- App updates that change the bundled backend re-sync the environment on the next start (fast when
  only our source changed). Switching hardware re-runs the install with another extra.
- Uninstalling the app removes the Python environment but never the data or models folders.
- Models are downloaded afterwards from the SPA (Settings → local models), as for any backend.

## Portable mode

Releases are portable zips (D76). A `portable` marker file next to the exe puts all app state in
the app folder instead of `%APPDATA%` / `%LOCALAPPDATA%`: app settings, logs and the default
recordings folder, and in the backend edition the managed environment, its data and the models
(the wizard can still put models elsewhere). Without the marker the per-user locations are used.
Deleting a portable folder deletes everything stored inside it.

