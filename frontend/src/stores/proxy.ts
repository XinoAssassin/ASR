/**
 * Desktop proxy setting (docs/desktop-ipc.md "Proxy"): system / none / manual, used by the managed
 * setup, the sidecar (on its next start) and the uploader. The core masks credentials in what it
 * returns; a masked URL sent back unchanged keeps the stored ones, so the UI never needs them in
 * clear. An older core without the commands: `supported` false and the proxy UI stays hidden.
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import {
  getDesktop,
  ipcErrorMessage,
  isDesktop,
  type EffectiveProxy,
  type ProxySettings,
  type ProxyTestResult,
} from '@/platform'

export const DEFAULT_NO_PROXY = 'localhost,127.0.0.1,::1,10.*,172.16.*-172.31.*,192.168.*,*.local'

export function defaultProxySettings(): ProxySettings {
  return { mode: 'system', url: null, noProxy: DEFAULT_NO_PROXY }
}

const SCHEMES = new Set(['http:', 'https:', 'socks5:'])

/**
 * Validate a manual proxy URL: http / https / socks5 with a host, no path / query. Credentials
 * (also the core's masked `user:***@`) are fine. Returns an error key (`proxy.errors.*`) or null.
 */
export function proxyUrlError(url: string | null | undefined): 'required' | 'invalid' | 'scheme' | null {
  const text = url?.trim() ?? ''
  if (!text) return 'required'
  const scheme = /^([a-z][a-z0-9+.-]*):\/\//i.exec(text)?.[1]
  if (!scheme) return 'invalid'
  if (!SCHEMES.has(`${scheme.toLowerCase()}:`)) return 'scheme'
  let parsed: URL
  try {
    // URL() only knows the host of special schemes: parse socks5 as http.
    parsed = new URL(text.replace(/^socks5:/i, 'http:'))
  } catch {
    return 'invalid'
  }
  if (!parsed.hostname || /\s/.test(text)) return 'invalid'
  if ((parsed.pathname && parsed.pathname !== '/') || parsed.search || parsed.hash) return 'invalid'
  return null
}

/** " a, ,b ,c" → "a,b,c". */
export function cleanNoProxy(list: string): string {
  return list
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
    .join(',')
}

/** The settings to send: the URL only in manual mode, trimmed; a clean no-proxy list. */
export function cleanProxySettings(s: ProxySettings): ProxySettings {
  return {
    mode: s.mode,
    url: s.mode === 'manual' ? s.url?.trim() || null : null,
    noProxy: cleanNoProxy(s.noProxy) || DEFAULT_NO_PROXY,
  }
}

export function sameProxySettings(a: ProxySettings, b: ProxySettings): boolean {
  const x = cleanProxySettings(a)
  const y = cleanProxySettings(b)
  return x.mode === y.mode && x.url === y.url && x.noProxy === y.noProxy
}

export const useProxyStore = defineStore('proxy', () => {
  const available = isDesktop()
  /** null until loaded; false for an older core without the proxy commands. */
  const supported = ref<boolean | null>(available ? null : false)
  const settings = ref<ProxySettings>(defaultProxySettings())
  const effective = ref<EffectiveProxy | null>(null)
  const loading = ref(false)
  const saving = ref(false)
  const testing = ref(false)
  const error = ref<string | null>(null)

  let loadPromise: Promise<void> | null = null

  /** Load once (idempotent); `force` re-reads, e.g. after the system settings changed. */
  function load(force = false): Promise<void> {
    if (!available) return Promise.resolve()
    if (loadPromise && !force) return loadPromise
    loading.value = true
    loadPromise = (async () => {
      try {
        const state = await (await getDesktop()).network.getProxy()
        settings.value = state.settings
        effective.value = state.effective
        supported.value = true
        error.value = null
      } catch (e) {
        supported.value = false
        error.value = ipcErrorMessage(e)
      } finally {
        loading.value = false
      }
    })()
    return loadPromise
  }

  /** Throws the IPC error. */
  async function save(next: ProxySettings): Promise<void> {
    saving.value = true
    try {
      const state = await (await getDesktop()).network.setProxy(cleanProxySettings(next))
      settings.value = state.settings
      effective.value = state.effective
    } catch (e) {
      throw new Error(ipcErrorMessage(e))
    } finally {
      saving.value = false
    }
  }

  /** Test `candidate` (unsaved) or the saved settings. Throws the IPC error. */
  async function test(candidate?: ProxySettings): Promise<ProxyTestResult> {
    testing.value = true
    try {
      return await (await getDesktop()).network.testProxy(candidate && cleanProxySettings(candidate))
    } catch (e) {
      throw new Error(ipcErrorMessage(e))
    } finally {
      testing.value = false
    }
  }

  const ready = computed(() => supported.value === true)

  return { available, supported, ready, settings, effective, loading, saving, testing, error, load, save, test }
})
