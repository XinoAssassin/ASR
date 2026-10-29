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
}

export interface StartOptions {
  system: boolean
  mic: boolean
  /** Device.id from devices().outputs; null = default. */
  outputDevice: string | null
  inputDevice: string | null
  /** null = <Documents>/ASR Recordings. */
  outDir: string | null
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
  }
}
