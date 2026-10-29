/**
 * Desktop "local backend" sidecar (M10, docs/desktop-ipc.md): the optional `asr serve` process the
 * desktop app can start. Config + live status (`sidecar://status`), start / stop, and "use this
 * backend", which points the SPA (browser setting) and the Rust uploader at it.
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'

import {
  getDesktop,
  ipcErrorMessage,
  isDesktop,
  type SidecarConfig,
  type SidecarStatus,
  type Unlisten,
} from '@/platform'

import { useRecorderStore } from './recorder'
import { useSettingsStore } from './settings'

export const DEFAULT_SIDECAR_PORT = 8000

export function defaultSidecarConfig(): SidecarConfig {
  return {
    enabled: false,
    command: '',
    args: ['serve', '--port', String(DEFAULT_SIDECAR_PORT)],
    cwd: null,
    port: DEFAULT_SIDECAR_PORT,
  }
}

/** Keep a `--port N` / `--port=N` argument in step with the port field (other args untouched). */
export function syncPortArg(args: readonly string[], port: number): string[] {
  const out = [...args]
  const i = out.findIndex((a) => a === '--port' || a.startsWith('--port='))
  if (i === -1) return out
  if (out[i] === '--port') {
    if (i + 1 < out.length) out[i + 1] = String(port)
    else out.push(String(port))
  } else {
    out[i] = `--port=${port}`
  }
  return out
}

/** Clean a config from the form: trimmed strings, no empty args, null cwd when blank. */
export function cleanSidecarConfig(c: SidecarConfig): SidecarConfig {
  const port = Math.round(Number(c.port))
  return {
    enabled: c.enabled,
    command: c.command.trim(),
    args: c.args.map((a) => a.trim()).filter(Boolean),
    cwd: c.cwd?.trim() || null,
    port: Number.isFinite(port) && port > 0 && port < 65536 ? port : DEFAULT_SIDECAR_PORT,
  }
}

const STOPPED: SidecarStatus = { state: 'stopped', pid: null, url: null, error: null }

export const useSidecarStore = defineStore('sidecar', () => {
  const available = isDesktop()
  const config = ref<SidecarConfig | null>(null)
  const status = ref<SidecarStatus>({ ...STOPPED })
  const saving = ref(false)
  const starting = ref(false)
  const stopping = ref(false)
  /** Load error, e.g. an older core without the sidecar commands. */
  const error = ref<string | null>(null)

  let unlisten: Unlisten | null = null
  let initPromise: Promise<void> | null = null

  function init(): Promise<void> {
    if (!available) return Promise.resolve()
    initPromise ??= (async () => {
      const platform = await getDesktop()
      unlisten = await platform.events.onSidecarStatus((s) => (status.value = s))
      const [c, s] = await Promise.all([platform.sidecar.getConfig(), platform.sidecar.status()])
      config.value = c
      status.value = s
      error.value = null
    })().catch((e: unknown) => {
      initPromise = null
      error.value = ipcErrorMessage(e)
    })
    return initPromise
  }

  function dispose(): void {
    unlisten?.()
    unlisten = null
    initPromise = null
  }

  /** Throws the IPC error message. */
  async function saveConfig(next: SidecarConfig): Promise<SidecarConfig> {
    saving.value = true
    try {
      config.value = await (await getDesktop()).sidecar.setConfig(cleanSidecarConfig(next))
      return config.value
    } catch (e) {
      throw new Error(ipcErrorMessage(e))
    } finally {
      saving.value = false
    }
  }

  /** Resolves once healthy or failed (the core times out after 120 s). Throws IPC errors. */
  async function start(): Promise<SidecarStatus> {
    starting.value = true
    try {
      status.value = await (await getDesktop()).sidecar.start()
      return status.value
    } catch (e) {
      throw new Error(ipcErrorMessage(e))
    } finally {
      starting.value = false
    }
  }

  async function stop(): Promise<SidecarStatus> {
    stopping.value = true
    try {
      status.value = await (await getDesktop()).sidecar.stop()
      return status.value
    } catch (e) {
      throw new Error(ipcErrorMessage(e))
    } finally {
      stopping.value = false
    }
  }

  /**
   * Point the SPA (Settings → backend URL override) and the Rust uploader at the running sidecar.
   * The browser setting stays the single source of truth; `syncBackend()` pushes it to Rust.
   */
  async function useAsBackend(): Promise<string> {
    const url = status.value.url
    if (status.value.state !== 'running' || !url) throw new Error('sidecar is not running')
    const settings = useSettingsStore()
    settings.setBackendUrl(url)
    await useRecorderStore().syncBackend()
    return url
  }

  return {
    available,
    config,
    status,
    saving,
    starting,
    stopping,
    error,
    init,
    dispose,
    saveConfig,
    start,
    stop,
    useAsBackend,
  }
})
