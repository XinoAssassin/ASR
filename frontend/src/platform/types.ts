/**
 * Types of the Tauri IPC contract. Mirrors `docs/desktop-ipc.md` exactly — change that file
 * (through the lead) before changing anything here. Times are seconds, levels are linear peak
 * amplitudes 0..1, sizes are bytes.
 */
import type { JobOptions, Term } from '@/api/endpoints'

export interface Device {
  id: string
  name: string
  isDefault: boolean
}

export interface DeviceList {
  outputs: Device[]
  inputs: Device[]
}

export type RecorderState = 'idle' | 'recording' | 'finalizing'

export interface RecorderStatus {
  state: RecorderState
  /** Seconds since start (0 when idle). */
  elapsed: number
  /** Peak since the last event. */
  levels: { system: number; mic: number }
  /** Path of the recording in progress / last finished one. */
  file: string | null
  /** Measured mic-vs-loopback drift, after resampling. */
  driftMs: number | null
  warnings: string[]
  /** What is actually captured (a missing mic falls back to system-only with a warning). */
  sources: { system: boolean; mic: boolean }
  /** Process tree being captured; null = whole device. */
  processId: number | null
}

export interface StartOptions {
  system: boolean
  mic: boolean
  /** Device.id from devices().outputs; null = default. */
  outputDevice: string | null
  inputDevice: string | null
  /** null = <Documents>/ASR Recordings. */
  outDir: string | null
  /** Capture only this process tree (Windows 10 2004+); null / absent = the whole render device. */
  processId?: number | null
}

/** A process with an active audio session (`recorder_processes`). */
export interface AudioProcess {
  pid: number
  name: string
  title: string | null
}

/** Optional local backend started by the app. */
export interface SidecarConfig {
  /** Start with the app, stop on quit. */
  enabled: boolean
  /** e.g. D:\ASR\backend\.venv\Scripts\asr.exe */
  command: string
  /** Default ["serve", "--port", "8000"]. */
  args: string[]
  cwd: string | null
  /** Health checked at http://127.0.0.1:{port}/api/health. */
  port: number
}

export type SidecarState = 'stopped' | 'starting' | 'running' | 'failed'

export interface SidecarStatus {
  state: SidecarState
  pid: number | null
  /** http://127.0.0.1:{port} while running. */
  url: string | null
  error: string | null
}

export type UploadState = 'pending' | 'uploading' | 'uploaded' | 'transcribing' | 'done' | 'failed'

export interface Recording {
  id: string
  path: string
  name: string
  size: number
  duration: number
  /** ISO 8601. */
  createdAt: string
  upload: {
    state: UploadState
    attempts: number
    uploadId: string | null
    jobId: string | null
    /** Set once the job succeeded. */
    transcriptId: string | null
    /** Last error, redacted (no keys / signed URLs). */
    error: string | null
    nextRetryAt: string | null
  }
}

export interface BackendConfig {
  /** e.g. http://127.0.0.1:8000 (no trailing slash). */
  baseUrl: string
  /** Sent as X-API-Key. Comes back masked as "***" when set; send "***" to keep the stored key. */
  apiKey: string | null
  /** Upload target_dir on the server. */
  targetDir: string
  autoUpload: boolean
  /** body.options for POST /api/jobs; null = upload only, no job. */
  jobOptions: JobOptions | null
  provider: 'local' | 'cloud' | null
  /** body.term_set_ids for POST /api/jobs. */
  termSetIds: string[]
  /** body.extra_terms (Term objects). */
  extraTerms: Term[]
}

export type ManagedHardware = 'cpu' | 'cuda' | 'rocm'

export type ManagedState = 'not_installed' | 'installing' | 'installed' | 'failed'

/** "Desktop + backend" edition: the backend environment the app creates and runs itself. */
export interface ManagedBackendStatus {
  /** external = plain recorder build (no bundled backend). */
  edition: 'bundled' | 'external'
  state: ManagedState
  /** What the environment was built for. */
  hardware: ManagedHardware | null
  /** Bundled backend source version / hash. */
  backendVersion: string | null
  /** ASR_DATA_DIR of the managed backend (asr.db, uploads). */
  dataDir: string | null
  /** ASR_MODEL_CACHE_DIR (large; may live on another drive). */
  modelsDir: string | null
  /** Python environment size on disk. */
  envSizeBytes: number | null
  /** Where dataDir goes when not chosen (portable: inside the app folder). */
  defaultDataDir: string | null
  defaultModelsDir: string | null
  /** App state lives next to the exe (portable marker present). */
  portable: boolean
  /** Last failure, redacted. */
  error: string | null
}

export interface HardwareSuggestion {
  id: ManagedHardware
  /** A matching GPU / build exists for this PC. */
  available: boolean
  recommended: boolean
  /** e.g. "AMD Radeon 8060S (ROCm, gfx1151)" */
  label: string
  /** Why not available. */
  detail: string | null
  /** Approximate one-time download for this choice. */
  downloadBytes: number | null
}

/** Folder of the managed backend that `managed_backend_reveal` opens. */
export type ManagedFolder = 'data' | 'models' | 'backend'

export interface PickFolderOptions {
  title?: string
  defaultPath?: string | null
}

export interface ManagedInstallOptions {
  hardware: ManagedHardware
  /** null = default under %LOCALAPPDATA%. */
  dataDir?: string | null
  modelsDir?: string | null
}

export type ManagedPhase = 'prepare' | 'python' | 'packages' | 'finalize' | 'done' | 'failed'

export interface ManagedProgress {
  phase: ManagedPhase
  /** Bilingual short status. */
  message: string
  /** Overall 0..100 when known. */
  percent: number | null
  /** Raw installer output line (redacted), for a log view. */
  logLine: string | null
}

/** Value of `BackendConfig.apiKey` meaning "a key is stored" (read) / "keep the stored key" (write). */
export const MASKED_KEY = '***'

export type Unlisten = () => void

/** Typed facade over the Rust core; implemented by `./tauri.ts` (and by mocks in tests). */
export interface DesktopPlatform {
  recorder: {
    devices(): Promise<DeviceList>
    start(options: StartOptions): Promise<RecorderStatus>
    stop(): Promise<Recording>
    status(): Promise<RecorderStatus>
    processes(): Promise<AudioProcess[]>
    /** Store the options the tray / Ctrl+Alt+R toggle replays, without starting. */
    setOptions(options: StartOptions): Promise<void>
  }
  sidecar: {
    getConfig(): Promise<SidecarConfig>
    /** Does not start / stop by itself. */
    setConfig(config: SidecarConfig): Promise<SidecarConfig>
    status(): Promise<SidecarStatus>
    /** Resolves once healthy or failed (timeout 120 s). */
    start(): Promise<SidecarStatus>
    stop(): Promise<SidecarStatus>
  }
  /** "Desktop + backend" edition; an older core rejects these (treat as `edition: 'external'`). */
  managedBackend: {
    status(): Promise<ManagedBackendStatus>
    detect(): Promise<HardwareSuggestion[]>
    /** Starts (or re-runs) the setup in the background; error if already installing. */
    install(options: ManagedInstallOptions): Promise<ManagedBackendStatus>
    /**
     * Stops a running setup (partial environment removed; data and models kept). A re-install /
     * hardware switch keeps the previous working environment (`installed`).
     */
    cancel(): Promise<ManagedBackendStatus>
    /** Open that folder in Explorer. */
    reveal(which: ManagedFolder): Promise<void>
  }
  dialog: {
    /** Native folder picker; null when cancelled. */
    pickFolder(options?: PickFolderOptions): Promise<string | null>
  }
  backendConfig: {
    get(): Promise<BackendConfig>
    set(config: BackendConfig): Promise<BackendConfig>
  }
  recordings: {
    list(): Promise<Recording[]>
    upload(id: string): Promise<Recording>
    reveal(id: string): Promise<void>
  }
  events: {
    /** `recorder://status` */
    onRecorderStatus(handler: (status: RecorderStatus) => void): Promise<Unlisten>
    /** `recordings://changed` */
    onRecordingChanged(handler: (recording: Recording) => void): Promise<Unlisten>
    /** `sidecar://status` */
    onSidecarStatus(handler: (status: SidecarStatus) => void): Promise<Unlisten>
    /** `managed-backend://progress` */
    onManagedProgress(handler: (progress: ManagedProgress) => void): Promise<Unlisten>
    /** `managed-backend://status` */
    onManagedStatus(handler: (status: ManagedBackendStatus) => void): Promise<Unlisten>
  }
}
