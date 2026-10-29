/**
 * Two kinds of settings: browser-local (backend URL override, X-API-Key — localStorage) and
 * server-side (`GET/PUT /api/settings`; the DashScope key is write-only and comes back masked).
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import {
  getApiKey,
  getBackendUrlOverride,
  getSettings,
  readStorage,
  setApiKey as persistApiKey,
  setBackendUrl as persistBackendUrl,
  testCloud as testCloudConnection,
  toApiError,
  updateSettings,
  writeStorage,
  type ApiError,
  type ConnectionTestResult,
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

  /** Server default provider (settings table, else ASR_PROVIDER_DEFAULT). */
  const defaultProvider = computed<ProviderName>(() => server.value?.provider_default ?? 'cloud')
  const cloudMaxFileSizeMb = computed(() => lastMaxMb.value ?? DEFAULT_CLOUD_MAX_FILE_SIZE_MB)

  async function load(): Promise<void> {
    loading.value = true
    try {
      server.value = await getSettings()
      error.value = null
    } catch (e) {
      error.value = toApiError(e)
    } finally {
      loading.value = false
    }
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

  function setBackendUrl(url: string): void {
    persistBackendUrl(url)
    backendUrl.value = getBackendUrlOverride()
    server.value = null
    void health.check()
  }

  function setApiKey(key: string): void {
    persistApiKey(key)
    apiKey.value = getApiKey() ?? ''
    health.clearUnauthorized()
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
    load,
    save,
    testCloud,
    setBackendUrl,
    setApiKey,
  }
})
