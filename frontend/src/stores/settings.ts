/**
 * Two kinds of settings: browser-local (the active backend profile's URL + X-API-Key —
 * localStorage, see `useProfilesStore` for the profiles themselves) and
 * server-side (`GET/PUT /api/settings`; the DashScope key is write-only and comes back masked),
 * plus the local model list shown on the Settings page (downloads only on explicit request, rule 6).
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import {
  backendOrigin,
  downloadLocalModel as requestModelDownload,
  getApiKey,
  getBackendUrlOverride,
  getSettings,
  listLocalModels,
  readStorage,
  setApiKey as persistApiKey,
  setBackendUrl as persistBackendUrl,
  testCloud as testCloudConnection,
  toApiError,
  updateSettings,
  writeStorage,
  type ApiError,
  type ConnectionTestResult,
  type LocalModelOut,
  type ProviderName,
  type SettingsOut,
  type SettingsUpdate,
} from '@/api'
import { DEFAULT_CLOUD_MAX_FILE_SIZE_MB } from '@/utils/audio'

import { useHealthStore } from './health'

const CLOUD_MAX_MB_KEY = 'asr.cloudMaxFileSizeMb'

function storedMaxMb(): number | null {
  const raw = Number(readStorage(CLOUD_MAX_MB_KEY))
  return Number.isFinite(raw) && raw > 0 ? raw : null
}

export const useSettingsStore = defineStore('settings', () => {
  const health = useHealthStore()

  const backendUrl = ref(getBackendUrlOverride())
  const apiKey = ref(getApiKey() ?? '')

  const server = ref<SettingsOut | null>(null)
  const loading = ref(false)
  const saving = ref(false)
  const error = ref<ApiError | null>(null)

  const cloudTest = ref<ConnectionTestResult | null>(null)
  const testing = ref(false)
  const lastMaxMb = ref<number | null>(storedMaxMb())

  const localModels = ref<LocalModelOut[]>([])
  const localModelsError = ref<ApiError | null>(null)
  const anyDownloading = computed(() => localModels.value.some((m) => m.status === 'downloading'))

  /** Server default provider (settings table, else ASR_PROVIDER_DEFAULT). */
  const defaultProvider = computed<ProviderName>(() => server.value?.provider_default ?? 'cloud')
  const cloudMaxFileSizeMb = computed(() => lastMaxMb.value ?? DEFAULT_CLOUD_MAX_FILE_SIZE_MB)

  /** Bumped when the backend changes: answers of the previous one are ignored. */
  let epoch = 0

  async function load(): Promise<void> {
    const mine = epoch
    loading.value = true
    try {
      const result = await getSettings()
      if (mine !== epoch) return
      server.value = result
      error.value = null
    } catch (e) {
      if (mine === epoch) error.value = toApiError(e)
    } finally {
      if (mine === epoch) loading.value = false
    }
  }

  async function loadLocalModels(): Promise<void> {
    const mine = epoch
    try {
      const result = await listLocalModels()
      if (mine !== epoch) return
      localModels.value = result
      localModelsError.value = null
    } catch (e) {
      if (mine === epoch) localModelsError.value = toApiError(e)
    }
  }

  /** Re-read the active profile's URL / key (after a profile switch or edit). */
  function refreshConnection(): void {
    backendUrl.value = getBackendUrlOverride()
    apiKey.value = getApiKey() ?? ''
  }

  /** Another backend (profile switch): drop what the previous one said. */
  function resetForBackend(): void {
    epoch++
    refreshConnection()
    server.value = null
    loading.value = false
    error.value = null
    cloudTest.value = null
    localModels.value = []
    localModelsError.value = null
  }

  /** Start a model download (explicit user action); throws ApiError. Refreshes the list. */
  async function downloadLocalModel(key: string, mirror: boolean): Promise<void> {
    await requestModelDownload(key, mirror)
    await loadLocalModels()
  }

  /** Throws ApiError so the caller can show it. */
  async function save(update: SettingsUpdate): Promise<SettingsOut> {
    saving.value = true
    try {
      server.value = await updateSettings(update)
      error.value = null
      return server.value
    } finally {
      saving.value = false
    }
  }

  async function testCloud(): Promise<ConnectionTestResult> {
    testing.value = true
    try {
      const result = await testCloudConnection()
      cloudTest.value = result
      if (result.max_file_size_mb) {
        lastMaxMb.value = result.max_file_size_mb
        writeStorage(CLOUD_MAX_MB_KEY, String(result.max_file_size_mb))
      }
      return result
    } finally {
      testing.value = false
    }
  }

  /**
   * Whether saving `url` would send requests to another origin while an API key is stored: the
   * key was meant for the current backend, so the Settings page offers to clear it first.
   * Throws BackendUrlError for an invalid URL.
   */
  function keyAtRisk(url: string): boolean {
    if (!apiKey.value) return false
    return backendOrigin(url) !== backendOrigin(backendUrl.value)
  }

  /**
   * Throws BackendUrlError (nothing saved) for a non-http(s) URL; `gpu:8000` gets `http://`.
   * `clearApiKey` forgets the stored key in the same step (pointing at another host).
   */
  function setBackendUrl(url: string, options: { clearApiKey?: boolean } = {}): void {
    persistBackendUrl(url)
    if (options.clearApiKey) {
      persistApiKey(null)
      apiKey.value = ''
    }
    backendUrl.value = getBackendUrlOverride()
    epoch++
    server.value = null
    // A 401 from the previous backend says nothing about this one.
    health.clearUnauthorized()
    void health.check()
  }

  function setApiKey(key: string): void {
    persistApiKey(key)
    apiKey.value = getApiKey() ?? ''
    health.clearUnauthorized()
  }

  function clearApiKey(): void {
    setApiKey('')
  }

  return {
    backendUrl,
    apiKey,
    server,
    loading,
    saving,
    error,
    cloudTest,
    testing,
    defaultProvider,
    cloudMaxFileSizeMb,
    localModels,
    localModelsError,
    anyDownloading,
    load,
    loadLocalModels,
    refreshConnection,
    resetForBackend,
    downloadLocalModel,
    save,
    testCloud,
    keyAtRisk,
    setBackendUrl,
    setApiKey,
    clearApiKey,
  }
})
