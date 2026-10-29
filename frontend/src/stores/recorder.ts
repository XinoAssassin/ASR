/**
 * Desktop recorder state (Tauri only): live status from `recorder://status`, devices, start
 * options, the recordings list kept current by `recordings://changed`, and the backend config
 * that Rust uses for its own uploads. Everything goes through `@/platform` (docs/desktop-ipc.md).
 *
 * The browser settings (backend URL + X-API-Key in localStorage) are the single source of truth
 * for the connection; `syncBackend()` pushes them to Rust.
 */
import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'

import { getApiKey, getBackendUrl, getJob, readStorage, writeStorage } from '@/api'
import {
  DESKTOP_DEFAULT_BACKEND,
  getDesktop,
  ipcErrorMessage,
  isDesktop,
  type BackendConfig,
  type DeviceList,
  type Recording,
  type RecorderStatus,
  type StartOptions,
  type Unlisten,
} from '@/platform'

const OPTIONS_KEY = 'asr.recorder.startOptions'

export { DESKTOP_DEFAULT_BACKEND }

export const IDLE_STATUS: RecorderStatus = {
  state: 'idle',
  elapsed: 0,
  levels: { system: 0, mic: 0 },
  file: null,
  driftMs: null,
  warnings: [],
}

export function defaultStartOptions(): StartOptions {
  return { system: true, mic: true, outputDevice: null, inputDevice: null, outDir: null }
}

function readStartOptions(): StartOptions {
  const defaults = defaultStartOptions()
  try {
    const raw: unknown = JSON.parse(readStorage(OPTIONS_KEY) ?? 'null')
    if (!raw || typeof raw !== 'object') return defaults
    const o = raw as Partial<Record<keyof StartOptions, unknown>>
    const str = (v: unknown): string | null => (typeof v === 'string' && v ? v : null)
    return {
      system: typeof o.system === 'boolean' ? o.system : defaults.system,
      mic: typeof o.mic === 'boolean' ? o.mic : defaults.mic,
      outputDevice: str(o.outputDevice),
      inputDevice: str(o.inputDevice),
      outDir: str(o.outDir),
    }
  } catch {
    return defaults
  }
}

/** Newest first, as `recordings_list` returns them. */
function byNewest(a: Recording, b: Recording): number {
  return Date.parse(b.createdAt) - Date.parse(a.createdAt) || b.id.localeCompare(a.id)
}

/** Backend URL Rust should upload to (on desktop getBackendUrl() already defaults to it). */
export function desktopBackendUrl(): string {
  return getBackendUrl() || DESKTOP_DEFAULT_BACKEND
}

export const useRecorderStore = defineStore('recorder', () => {
  const available = isDesktop()

  const status = ref<RecorderStatus>({ ...IDLE_STATUS })
  const devices = ref<DeviceList>({ outputs: [], inputs: [] })
  const recordings = ref<Recording[]>([])
  const config = ref<BackendConfig | null>(null)
  const options = ref<StartOptions>(readStartOptions())
  /** jobId → transcriptId, filled on demand for finished jobs. */
  const transcriptIds = ref<Record<string, string>>({})

  const initialized = ref(false)
  const busy = ref(false)
  const loadingDevices = ref(false)
  const loadingRecordings = ref(false)
  const savingConfig = ref(false)
  /** Last IPC error (start / stop / load), shown on the recorder page. */
  const error = ref<string | null>(null)

  let unlisteners: Unlisten[] = []
  let initPromise: Promise<void> | null = null

  const recording = computed(() => status.value.state === 'recording')
  const active = computed(() => status.value.state !== 'idle')
  const canStart = computed(
    () => available && !busy.value && !active.value && (options.value.system || options.value.mic),
  )
  const keySet = computed(() => !!config.value?.apiKey)

  watch(options, (o) => writeStorage(OPTIONS_KEY, JSON.stringify(o)), { deep: true })

  function upsertRecording(rec: Recording): void {
    const rest = recordings.value.filter((r) => r.id !== rec.id)
    recordings.value = [rec, ...rest].sort(byNewest)
  }

  async function refreshDevices(): Promise<void> {
    loadingDevices.value = true
    try {
      devices.value = await (await getDesktop()).recorder.devices()
      // Forget a saved device that is gone (unplugged headset): fall back to the default.
      const { outputDevice, inputDevice } = options.value
      if (outputDevice && !devices.value.outputs.some((d) => d.id === outputDevice)) {
        options.value.outputDevice = null
      }
      if (inputDevice && !devices.value.inputs.some((d) => d.id === inputDevice)) {
        options.value.inputDevice = null
      }
    } finally {
      loadingDevices.value = false
    }
  }

  async function refreshRecordings(): Promise<void> {
    loadingRecordings.value = true
    try {
      recordings.value = [...(await (await getDesktop()).recordings.list())].sort(byNewest)
    } finally {
      loadingRecordings.value = false
    }
  }

  async function refreshStatus(): Promise<void> {
    status.value = await (await getDesktop()).recorder.status()
  }

  async function loadConfig(): Promise<BackendConfig> {
    config.value = await (await getDesktop()).backendConfig.get()
    return config.value
  }

  /** Whether Rust's connection differs from the browser settings (key compared by presence). */
  function connectionOutOfSync(c: BackendConfig): boolean {
    return c.baseUrl !== desktopBackendUrl() || !!c.apiKey !== !!getApiKey()
  }

  /** Subscribe to events and load everything. Idempotent; a no-op in the web build. */
  function init(): Promise<void> {
    if (!available) return Promise.resolve()
    initPromise ??= (async () => {
      const platform = await getDesktop()
      unlisteners = await Promise.all([
        platform.events.onRecorderStatus((s) => (status.value = s)),
        platform.events.onRecordingChanged(upsertRecording),
      ])
      const results = await Promise.allSettled([
        refreshStatus(),
        refreshDevices(),
        refreshRecordings(),
        loadConfig(),
      ])
      const failed = results.find((r): r is PromiseRejectedResult => r.status === 'rejected')
      error.value = failed ? ipcErrorMessage(failed.reason) : null
      // First run (or URL / key changed in another window): hand Rust the browser connection.
      if (config.value && connectionOutOfSync(config.value)) {
        try {
          await syncBackend()
        } catch (e) {
          error.value = ipcErrorMessage(e)
        }
      }
      initialized.value = true
    })().catch((e: unknown) => {
      initPromise = null
      error.value = ipcErrorMessage(e)
    })
    return initPromise
  }

  function dispose(): void {
    for (const off of unlisteners) off()
    unlisteners = []
    initPromise = null
    initialized.value = false
  }

  async function start(): Promise<void> {
    if (!canStart.value) return
    busy.value = true
    try {
      status.value = await (await getDesktop()).recorder.start({ ...options.value })
      error.value = null
    } catch (e) {
      error.value = ipcErrorMessage(e)
    } finally {
      busy.value = false
    }
  }

  async function stop(): Promise<Recording | null> {
    if (busy.value || status.value.state !== 'recording') return null
    busy.value = true
    try {
      const platform = await getDesktop()
      const rec = await platform.recorder.stop()
      upsertRecording(rec)
      error.value = null
      // The status event normally arrives first; read it in case it did not.
      if (status.value.state === 'recording') status.value = await platform.recorder.status()
      return rec
    } catch (e) {
      error.value = ipcErrorMessage(e)
      return null
    } finally {
      busy.value = false
    }
  }

  function toggle(): Promise<unknown> {
    return recording.value ? stop() : start()
  }

  /** Queue an upload now (manual upload or retry). Throws the IPC error message. */
  async function upload(id: string): Promise<Recording> {
    try {
      const rec = await (await getDesktop()).recordings.upload(id)
      upsertRecording(rec)
      return rec
    } catch (e) {
      throw new Error(ipcErrorMessage(e))
    }
  }

  async function reveal(id: string): Promise<void> {
    try {
      await (await getDesktop()).recordings.reveal(id)
    } catch (e) {
      throw new Error(ipcErrorMessage(e))
    }
  }

  /**
   * Write the backend config to Rust. `baseUrl` / `apiKey` always come from the browser
   * settings; the rest from `patch`, else the current config.
   */
  async function saveConfig(
    patch: Partial<Omit<BackendConfig, 'baseUrl' | 'apiKey'>> = {},
  ): Promise<BackendConfig> {
    savingConfig.value = true
    try {
      const platform = await getDesktop()
      const current = config.value ?? (await platform.backendConfig.get())
      const next: BackendConfig = {
        targetDir: current.targetDir,
        autoUpload: current.autoUpload,
        jobOptions: current.jobOptions,
        provider: current.provider,
        // Older cores may not send these yet.
        termSetIds: current.termSetIds ?? [],
        extraTerms: current.extraTerms ?? [],
        ...patch,
        baseUrl: desktopBackendUrl(),
        apiKey: getApiKey(),
      }
      config.value = await platform.backendConfig.set(next)
      return config.value
    } catch (e) {
      throw new Error(ipcErrorMessage(e))
    } finally {
      savingConfig.value = false
    }
  }

  /** Push the browser's backend URL + API key to Rust (after they change in Settings). */
  function syncBackend(): Promise<BackendConfig> {
    return saveConfig()
  }

  /** Transcript id for a finished job without `upload.transcriptId` (cached); null while unknown. */
  async function lookupTranscript(jobId: string): Promise<string | null> {
    const cached = transcriptIds.value[jobId]
    if (cached) return cached
    try {
      const job = await getJob(jobId)
      if (job.transcript_id) {
        transcriptIds.value = { ...transcriptIds.value, [jobId]: job.transcript_id }
        return job.transcript_id
      }
    } catch {
      // backend unreachable: the row keeps its link to the jobs page
    }
    return null
  }

  return {
    available,
    status,
    devices,
    recordings,
    config,
    options,
    transcriptIds,
    initialized,
    busy,
    loadingDevices,
    loadingRecordings,
    savingConfig,
    error,
    recording,
    active,
    canStart,
    keySet,
    init,
    dispose,
    refreshDevices,
    refreshRecordings,
    start,
    stop,
    toggle,
    upload,
    reveal,
    saveConfig,
    syncBackend,
    lookupTranscript,
  }
})

