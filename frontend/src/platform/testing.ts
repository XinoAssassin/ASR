/**
 * In-memory `DesktopPlatform` for unit tests (no Tauri). Records every call and lets the test
 * emit `recorder://status` / `recordings://changed` / `sidecar://status` events.
 * `state.missing` simulates an older core without some commands.
 *
 * Managed backend ("desktop + backend" edition): `state.managed` starts as the plain build
 * (`edition: 'external'`); use `bundledManagedStatus()` to simulate the bundled one. `install`
 * starts a scripted setup that the test drives with `tickInstall()` / `runInstall()` (one
 * `managed-backend://progress` event per tick); `state.installFailAt` makes it fail at that tick.
 * Like the Rust core, a successful setup writes + starts the sidecar before reporting `installed`,
 * and a cancelled / failed re-install keeps the previous working environment.
 *
 * Proxy: `state.proxy` holds what was saved (URL with credentials in clear, like the core's store);
 * reads return it masked, and sending the masked URL back keeps the stored credentials.
 * `state.systemProxy` is what `system` mode resolves to; `state.proxyTest` the test answer.
 */
import type {
  AudioProcess,
  BackendConfig,
  DesktopPlatform,
  DeviceList,
  HardwareSuggestion,
  ManagedBackendStatus,
  ManagedInstallOptions,
  ManagedProgress,
  ProxySettings,
  ProxyState,
  ProxyTestResult,
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
  emitManagedProgress(progress: ManagedProgress): void
  emitManagedStatus(status: ManagedBackendStatus): void
  /** Emit the next scripted progress event of a running setup; false when none is running. */
  tickInstall(): boolean
  /** Tick until the setup finished (or failed). */
  runInstall(): void
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
    /** What `recorder_set_options` / `recorder_start` stored for the tray / hotkey toggle. */
    lastOptions: StartOptions | null
    sidecarConfig: SidecarConfig
    sidecar: SidecarStatus
    /** Commands that reject like an older core would. */
    missing: Set<string>
    managed: ManagedBackendStatus
    suggestions: HardwareSuggestion[]
    /** Progress events of one setup (the final `done` / `failed` is added by the fake). */
    installScript: ManagedProgress[]
    /** Fail the setup at this tick (index into `installScript`); null = succeed. */
    installFailAt: number | null
    /** Next tick of the running setup. */
    installTick: number
    lastInstall: ManagedInstallOptions | null
    /** The working environment a re-install / hardware switch started from (kept on cancel / failure). */
    previous: ManagedBackendStatus | null
    /** Folder `pick_folder` returns (null = the user cancelled the dialog). */
    pickedFolder: string | null
    proxy: ProxySettings
    /** What "system" resolves to (registry / environment). */
    systemProxy: ProxyState['effective']
    proxyTest: ProxyTestResult
  }
}

export const DEFAULT_NO_PROXY = 'localhost,127.0.0.1,::1,10.*,172.16.*-172.31.*,192.168.*,*.local'

/** `http://user:secret@host:1` → `http://user:***@host:1` (as the core masks credentials). */
export function maskProxyUrl(url: string | null): string | null {
  if (!url) return url
  return url.replace(/^([a-z0-9]+:\/\/[^:@/]+):[^@/]*@/i, '$1:***@')
}

export const FAKE_MANAGED_PORT = 8765

export function defaultSuggestions(): HardwareSuggestion[] {
  return [
    {
      id: 'cpu',
      available: true,
      recommended: false,
      label: 'AMD Ryzen AI Max+ 395 (CPU)',
      detail: null,
      downloadBytes: 1_073_741_824,
    },
    {
      id: 'cuda',
      available: false,
      recommended: false,
      label: 'NVIDIA CUDA',
      detail: 'No NVIDIA GPU found / 未检测到 NVIDIA 显卡',
      downloadBytes: 2_684_354_560,
    },
    {
      id: 'rocm',
      available: true,
      recommended: true,
      label: 'AMD Radeon 8060S (ROCm, gfx1151)',
      detail: null,
      downloadBytes: 5_368_709_120,
    },
  ]
}

export function defaultInstallScript(): ManagedProgress[] {
  return [
    { phase: 'prepare', message: '准备安装 / Preparing', percent: 2, logLine: 'checking disk space' },
    { phase: 'python', message: '下载 Python 3.12 / Downloading Python 3.12', percent: 10, logLine: 'Installed Python 3.12.11' },
    { phase: 'packages', message: '安装依赖 / Installing packages', percent: 35, logLine: 'Downloading torch (812.3MiB)' },
    { phase: 'packages', message: '安装依赖 / Installing packages', percent: 70, logLine: 'Prepared 142 packages in 3m 12s' },
    { phase: 'finalize', message: '完成配置 / Finishing', percent: 95, logLine: 'Installed 142 packages in 41s' },
  ]
}

export function externalManagedStatus(): ManagedBackendStatus {
  return {
    edition: 'external',
    state: 'not_installed',
    hardware: null,
    backendVersion: null,
    dataDir: null,
    modelsDir: null,
    envSizeBytes: null,
    defaultDataDir: null,
    defaultModelsDir: null,
    portable: false,
    error: null,
  }
}

export const FAKE_DEFAULT_DATA_DIR = 'C:\\Users\\me\\AppData\\Local\\ASR\\data'
export const FAKE_DEFAULT_MODELS_DIR = 'C:\\Users\\me\\AppData\\Local\\ASR\\models'

/** The bundled edition before its first setup (per-user defaults; not portable). */
export function bundledManagedStatus(patch: Partial<ManagedBackendStatus> = {}): ManagedBackendStatus {
  return {
    ...externalManagedStatus(),
    edition: 'bundled',
    defaultDataDir: FAKE_DEFAULT_DATA_DIR,
    defaultModelsDir: FAKE_DEFAULT_MODELS_DIR,
    ...patch,
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
  const progressHandlers = new Set<(p: ManagedProgress) => void>()
  const managedHandlers = new Set<(s: ManagedBackendStatus) => void>()
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
    lastOptions: null,
    sidecarConfig: {
      enabled: false,
      command: '',
      args: ['serve', '--port', '8000'],
      cwd: null,
      port: 8000,
    },
    sidecar: { state: 'stopped', pid: null, url: null, error: null },
    missing: new Set(),
    managed: externalManagedStatus(),
    suggestions: defaultSuggestions(),
    installScript: defaultInstallScript(),
    installFailAt: null,
    installTick: 0,
    lastInstall: null,
    previous: null,
    pickedFolder: 'D:\\Picked\\Folder',
    proxy: { mode: 'system', url: null, noProxy: DEFAULT_NO_PROXY },
    systemProxy: {
      source: 'system',
      url: 'http://127.0.0.1:7890',
      noProxy: 'localhost,127.0.0.1,<local>',
      pacUrl: null,
      warnings: [],
    },
    proxyTest: { ok: true, message: 'pypi.org 200, modelscope.cn 200' },
  }

  function proxyState(): ProxyState {
    const p = state.proxy
    const effective: ProxyState['effective'] =
      p.mode === 'system'
        ? { ...state.systemProxy, warnings: [...state.systemProxy.warnings] }
        : p.mode === 'manual'
          ? { source: 'manual', url: maskProxyUrl(p.url), noProxy: p.noProxy, pacUrl: null, warnings: [] }
          : { source: 'none', url: null, noProxy: p.noProxy, pacUrl: null, warnings: [] }
    return { settings: { ...p, url: maskProxyUrl(p.url) }, effective }
  }

  const emitManagedStatus = (s: ManagedBackendStatus): void => managedHandlers.forEach((h) => h({ ...s }))
  const emitManagedProgress = (p: ManagedProgress): void => progressHandlers.forEach((h) => h({ ...p }))
  const setManaged = (patch: Partial<ManagedBackendStatus>): ManagedBackendStatus => {
    state.managed = { ...state.managed, ...patch }
    emitManagedStatus(state.managed)
    return { ...state.managed }
  }

  function tickInstall(): boolean {
    if (state.managed.state !== 'installing') return false
    const i = state.installTick++
    if (state.installFailAt != null && i >= state.installFailAt) {
      const error = 'uv sync failed: network unreachable / 网络不可用'
      emitManagedProgress({ phase: 'failed', message: '安装失败 / Setup failed', percent: null, logLine: `error: ${error}` })
      // A failed re-install / switch keeps the previous working environment.
      setManaged(state.previous ? { ...state.previous, error } : { state: 'failed', error })
      return true
    }
    const step = state.installScript[i]
    if (step) {
      emitManagedProgress(step)
      return true
    }
    // Like the Rust core: write + start the sidecar, then report done.
    const opts = state.lastInstall
    state.sidecarConfig = {
      enabled: true,
      command: 'C:\\Users\\me\\AppData\\Local\\ASR\\env\\Scripts\\asr.exe',
      args: ['serve', '--host', '127.0.0.1', '--port', String(FAKE_MANAGED_PORT)],
      cwd: null,
      port: FAKE_MANAGED_PORT,
    }
    state.sidecar = { state: 'running', pid: 999, url: `http://127.0.0.1:${FAKE_MANAGED_PORT}`, error: null }
    sidecarHandlers.forEach((h) => h({ ...state.sidecar }))
    emitManagedProgress({ phase: 'done', message: '安装完成 / Setup complete', percent: 100, logLine: null })
    setManaged({
      state: 'installed',
      hardware: opts?.hardware ?? 'cpu',
      backendVersion: '0.9.0+abc1234',
      dataDir: opts?.dataDir || state.managed.defaultDataDir,
      modelsDir: opts?.modelsDir || state.managed.defaultModelsDir,
      envSizeBytes: opts?.hardware === 'rocm' ? 5_368_709_120 : 1_288_490_189,
      error: null,
    })
    return true
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
          state.lastOptions = structuredClone(options)
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
      setOptions: (options: StartOptions) =>
        call('recorder_set_options', { options }, () => {
          state.lastOptions = structuredClone(options)
        }),
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
    managedBackend: {
      status: () => call('managed_backend_status', undefined, () => ({ ...state.managed })),
      detect: () => call('managed_backend_detect', undefined, () => structuredClone(state.suggestions)),
      install: (options: ManagedInstallOptions) =>
        call('managed_backend_install', { options }, () => {
          if (state.managed.edition !== 'bundled') throw 'not the bundled edition'
          if (state.managed.state === 'installing') throw 'already installing'
          state.lastInstall = structuredClone(options)
          state.installTick = 0
          state.previous = state.managed.state === 'installed' ? { ...state.managed, error: null } : null
          return setManaged({ state: 'installing', error: null })
        }),
      cancel: () =>
        call('managed_backend_cancel', undefined, () => {
          if (state.managed.state !== 'installing') return { ...state.managed }
          emitManagedProgress({ phase: 'failed', message: '已取消 / Cancelled', percent: null, logLine: 'cancelled' })
          // A cancelled re-install / switch keeps the previous working environment.
          return setManaged(
            state.previous ?? { state: 'not_installed', hardware: null, envSizeBytes: null, error: null },
          )
        }),
      reveal: (which) => call('managed_backend_reveal', { which }, () => undefined),
    },
    network: {
      getProxy: () => call('network_get_proxy', undefined, proxyState),
      setProxy: (settings: ProxySettings) =>
        call('network_set_proxy', { settings }, () => {
          if (settings.mode === 'manual' && !settings.url) throw 'manual proxy needs a URL'
          // The masked URL sent back keeps the stored credentials.
          const keep = settings.url != null && settings.url === maskProxyUrl(state.proxy.url)
          state.proxy = { ...settings, url: keep ? state.proxy.url : settings.url }
          return proxyState()
        }),
      testProxy: (settings?: ProxySettings) =>
        call('network_test_proxy', settings ? { settings } : {}, () => ({ ...state.proxyTest })),
    },
    dialog: {
      pickFolder: (options = {}) =>
        call('pick_folder', { title: options.title, defaultPath: options.defaultPath ?? null }, () => state.pickedFolder),
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
      onManagedProgress: async (handler) => {
        progressHandlers.add(handler)
        return () => progressHandlers.delete(handler)
      },
      onManagedStatus: async (handler) => {
        managedHandlers.add(handler)
        return () => managedHandlers.delete(handler)
      },
    },
    emitStatus: (s) => statusHandlers.forEach((h) => h(s)),
    emitRecording: (r) => recordingHandlers.forEach((h) => h(r)),
    emitSidecar: (s) => sidecarHandlers.forEach((h) => h(s)),
    emitManagedProgress,
    emitManagedStatus,
    tickInstall,
    runInstall: () => {
      for (let i = 0; i < 1000 && tickInstall(); i++);
    },
    listenerCount: () =>
      statusHandlers.size +
      recordingHandlers.size +
      sidecarHandlers.size +
      progressHandlers.size +
      managedHandlers.size,
  }
  return fake
}
