/**
 * In-memory `DesktopPlatform` for unit tests (no Tauri). Records every call and lets the test
 * emit `recorder://status` / `recordings://changed` / `sidecar://status` events.
 * `state.missing` simulates an older core without some commands.
 */
import type {
  AudioProcess,
  BackendConfig,
  DesktopPlatform,
  DeviceList,
  Recording,
  RecorderStatus,
  SidecarConfig,
  SidecarStatus,
  StartOptions,
} from './types'

export interface FakeDesktop extends DesktopPlatform {
  calls: Array<[string, unknown]>
  emitStatus(status: RecorderStatus): void
  emitRecording(recording: Recording): void
  emitSidecar(status: SidecarStatus): void
  listenerCount(): number
  state: {
    devices: DeviceList
    status: RecorderStatus
    recordings: Recording[]
    config: BackendConfig
    storedKey: string | null
    failNext: string | null
    processes: AudioProcess[]
    /** Pretend the mic cannot be opened: start falls back to system-only (M10). */
    noMic: boolean
    sidecarConfig: SidecarConfig
    sidecar: SidecarStatus
    /** Commands that reject like an older core would. */
    missing: Set<string>
  }
}

export function makeRecording(id: string, patch: Partial<Recording['upload']> = {}): Recording {
  return {
    id,
    path: `C:\\Users\\me\\Documents\\ASR Recordings\\${id}.flac`,
    name: `${id}.flac`,
    size: 1_234_567,
    duration: 75.5,
    createdAt: `${id.slice(0, 10)}T${id.slice(11).replace(/-/g, ':')}Z`,
    upload: {
      state: 'pending',
      attempts: 0,
      uploadId: null,
      jobId: null,
      transcriptId: null,
      error: null,
      nextRetryAt: null,
      ...patch,
    },
  }
}

export function createFakeDesktop(): FakeDesktop {
  const statusHandlers = new Set<(s: RecorderStatus) => void>()
  const recordingHandlers = new Set<(r: Recording) => void>()
  const sidecarHandlers = new Set<(s: SidecarStatus) => void>()
  const calls: Array<[string, unknown]> = []
  const state: FakeDesktop['state'] = {
    devices: {
      outputs: [
        { id: 'out-1', name: 'Speakers (Realtek)', isDefault: true },
        { id: 'out-2', name: 'Headphones (USB)', isDefault: false },
      ],
      inputs: [{ id: 'in-1', name: 'Microphone Array', isDefault: true }],
    },
    status: {
      state: 'idle',
      elapsed: 0,
      levels: { system: 0, mic: 0 },
      file: null,
      driftMs: null,
      warnings: [],
      sources: { system: false, mic: false },
      processId: null,
    },
    recordings: [makeRecording('2026-10-28_09-00-00', { state: 'done', jobId: 'job-1' })],
    config: {
      baseUrl: '',
      apiKey: null,
      targetDir: '',
      autoUpload: true,
      jobOptions: null,
      provider: null,
      termSetIds: [],
      extraTerms: [],
    },
    storedKey: null,
    failNext: null,
    processes: [
      { pid: 4242, name: 'Teams.exe', title: 'Weekly sync | Microsoft Teams' },
      { pid: 5150, name: 'chrome.exe', title: null },
    ],
    noMic: false,
    sidecarConfig: {
      enabled: false,
      command: '',
      args: ['serve', '--port', '8000'],
      cwd: null,
      port: 8000,
    },
    sidecar: { state: 'stopped', pid: null, url: null, error: null },
    missing: new Set(),
  }

  const masked = (): BackendConfig => ({ ...state.config, apiKey: state.storedKey ? '***' : null })

  async function call<T>(name: string, args: unknown, run: () => T): Promise<T> {
    calls.push([name, args])
    if (state.missing.has(name)) throw `Command ${name} not found`
    if (state.failNext) {
      const msg = state.failNext
      state.failNext = null
      throw msg // Tauri rejects with the Rust error string
    }
    return run()
  }

  const fake: FakeDesktop = {
    calls,
    state,
    recorder: {
      devices: () => call('recorder_devices', undefined, () => structuredClone(state.devices)),
      start: (options: StartOptions) =>
        call('recorder_start', { options }, () => {
          if (state.status.state !== 'idle') throw 'already recording'
          const mic = options.mic && !state.noMic
          state.status = {
            ...state.status,
            state: 'recording',
            file: 'x.partial.wav',
            sources: { system: options.system, mic },
            processId: options.processId ?? null,
            warnings: options.mic && !mic ? ['microphone unavailable, recording system audio only'] : [],
          }
          return structuredClone(state.status)
        }),
      stop: () =>
        call('recorder_stop', undefined, () => {
          const rec = makeRecording('2026-10-29_10-00-00')
          state.recordings.unshift(rec)
          state.status = {
            ...state.status,
            state: 'idle',
            elapsed: 0,
            sources: { system: false, mic: false },
            processId: null,
          }
          return rec
        }),
      status: () => call('recorder_status', undefined, () => structuredClone(state.status)),
      processes: () => call('recorder_processes', undefined, () => structuredClone(state.processes)),
    },
    sidecar: {
      getConfig: () => call('sidecar_get_config', undefined, () => structuredClone(state.sidecarConfig)),
      setConfig: (config: SidecarConfig) =>
        call('sidecar_set_config', { config }, () => {
          state.sidecarConfig = structuredClone(config)
          return structuredClone(config)
        }),
      status: () => call('sidecar_status', undefined, () => ({ ...state.sidecar })),
      start: () =>
        call('sidecar_start', undefined, () => {
          if (!state.sidecarConfig.command) {
            state.sidecar = { state: 'failed', pid: null, url: null, error: 'command is empty' }
          } else {
            const port = state.sidecarConfig.port
            state.sidecar = { state: 'running', pid: 777, url: `http://127.0.0.1:${port}`, error: null }
          }
          sidecarHandlers.forEach((h) => h({ ...state.sidecar }))
          return { ...state.sidecar }
        }),
      stop: () =>
        call('sidecar_stop', undefined, () => {
          state.sidecar = { state: 'stopped', pid: null, url: null, error: null }
          sidecarHandlers.forEach((h) => h({ ...state.sidecar }))
          return { ...state.sidecar }
        }),
    },
    backendConfig: {
      get: () => call('backend_get_config', undefined, masked),
      set: (config: BackendConfig) =>
        call('backend_set_config', { config }, () => {
          if (config.apiKey !== '***') state.storedKey = config.apiKey
          state.config = { ...config }
          return masked()
        }),
    },
    recordings: {
      list: () => call('recordings_list', undefined, () => structuredClone(state.recordings)),
      upload: (id: string) =>
        call('recording_upload', { id }, () => {
          const rec = state.recordings.find((r) => r.id === id)
          if (!rec) throw `no recording ${id}`
          rec.upload = { ...rec.upload, state: 'pending', error: null }
          return structuredClone(rec)
        }),
      reveal: (id: string) => call('recording_reveal', { id }, () => undefined),
    },
    events: {
      onRecorderStatus: async (handler) => {
        statusHandlers.add(handler)
        return () => statusHandlers.delete(handler)
      },
      onRecordingChanged: async (handler) => {
        recordingHandlers.add(handler)
        return () => recordingHandlers.delete(handler)
      },
      onSidecarStatus: async (handler) => {
        sidecarHandlers.add(handler)
        return () => sidecarHandlers.delete(handler)
      },
    },
    emitStatus: (s) => statusHandlers.forEach((h) => h(s)),
    emitRecording: (r) => recordingHandlers.forEach((h) => h(r)),
    emitSidecar: (s) => sidecarHandlers.forEach((h) => h(s)),
    listenerCount: () => statusHandlers.size + recordingHandlers.size + sidecarHandlers.size,
  }
  return fake
}
