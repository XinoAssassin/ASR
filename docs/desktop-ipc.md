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
| `recorder_processes` | — | `AudioProcess[]` | M10: processes with an active audio session, for `processId` |
| `sidecar_get_config` | — | `SidecarConfig` | M10 |
| `sidecar_set_config` | `{ config: SidecarConfig }` | `SidecarConfig` | M10; does not start / stop by itself |
| `sidecar_status` | — | `SidecarStatus` | M10 |
| `sidecar_start` | — | `SidecarStatus` | M10; resolves once healthy or failed (timeout 120 s) |
| `sidecar_stop` | — | `SidecarStatus` | M10 |

## Events (`listen(name)`)

| Event | Payload | Rate |
|---|---|---|
| `recorder://status` | `RecorderStatus` | ~10 Hz while recording, once on each state change |
| `recordings://changed` | `Recording` | On every upload state change |
| `sidecar://status` | `SidecarStatus` | M10: on every state change |

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

Upload state: a transient failure is `failed` with `nextRetryAt` set (it retries by itself); a
permanent one (4xx other than 408 / 429) has `nextRetryAt: null` and waits for `recording_upload`
or a config change. Warning strings are bilingual and shown verbatim.

## Backend notes

- Tauri 2 on Windows serves the SPA from `http://tauri.localhost`; the backend must list that
  origin in `ASR_CORS_ORIGINS` for the SPA's own API calls. Rust's uploads are not browser
  requests and need no CORS.
- With `ASR_API_KEY` empty the backend accepts only loopback Host names, so a remote backend needs
  a key.
