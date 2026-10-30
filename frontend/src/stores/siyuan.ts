/**
 * SiYuan (思源笔记) export (D61): the server-side SiYuan settings (the token is write-only and
 * comes back masked, like the DashScope key), the connection test, the notebook list and the
 * push of a transcript. The backend talks to SiYuan itself, so SiYuan must be reachable from the
 * backend host; a 502 carries that hint.
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import {
  getSiyuanSettings,
  listSiyuanNotebooks,
  pushToSiyuan,
  testSiyuan,
  toApiError,
  updateSiyuanSettings,
  type ApiError,
  type SiYuanNotebookOut,
  type SiYuanPushIn,
  type SiYuanPushOut,
  type SiYuanSettingsOut,
  type SiYuanSettingsUpdate,
  type SiYuanTestOut,
} from '@/api'

export const useSiyuanStore = defineStore('siyuan', () => {
  const settings = ref<SiYuanSettingsOut | null>(null)
  const loading = ref(false)
  const error = ref<ApiError | null>(null)
  const saving = ref(false)

  const testResult = ref<SiYuanTestOut | null>(null)
  const testing = ref(false)

  const notebooks = ref<SiYuanNotebookOut[]>([])
  const notebooksLoading = ref(false)
  const notebooksError = ref<ApiError | null>(null)

  const pushing = ref(false)

  const configured = computed(() => !!settings.value?.token_set)

  async function load(): Promise<void> {
    loading.value = true
    try {
      settings.value = await getSiyuanSettings()
      error.value = null
    } catch (e) {
      error.value = toApiError(e)
    } finally {
      loading.value = false
    }
  }

  /** `GET /api/siyuan/notebooks`; errors land in `notebooksError` (422 not configured, 502). */
  async function loadNotebooks(): Promise<void> {
    notebooksLoading.value = true
    try {
      notebooks.value = await listSiyuanNotebooks()
      notebooksError.value = null
    } catch (e) {
      notebooks.value = []
      notebooksError.value = toApiError(e)
    } finally {
      notebooksLoading.value = false
    }
  }

  /** Partial update; throws ApiError. A changed endpoint without a token clears the token. */
  async function save(update: SiYuanSettingsUpdate): Promise<SiYuanSettingsOut> {
    saving.value = true
    try {
      const before = settings.value
      const saved = await updateSiyuanSettings(update)
      settings.value = saved
      error.value = null
      const target = before?.endpoint !== saved.endpoint || update.token !== undefined
      if (target) testResult.value = null // it was about another endpoint / token
      if (!saved.token_set) {
        notebooks.value = []
        notebooksError.value = null
      } else if (target || !notebooks.value.length) {
        void loadNotebooks()
      }
      return saved
    } finally {
      saving.value = false
    }
  }

  /** Test the saved endpoint and token; on success the notebook list is (re)loaded. */
  async function test(): Promise<SiYuanTestOut> {
    testing.value = true
    try {
      const result = await testSiyuan()
      testResult.value = result
      if (result.ok) void loadNotebooks()
      return result
    } finally {
      testing.value = false
    }
  }

  /** Create a SiYuan document from the transcript's current segments. Throws ApiError. */
  async function push(transcriptId: string, body: SiYuanPushIn): Promise<SiYuanPushOut> {
    pushing.value = true
    try {
      return await pushToSiyuan(transcriptId, body)
    } finally {
      pushing.value = false
    }
  }

  return {
    settings,
    loading,
    error,
    saving,
    testResult,
    testing,
    notebooks,
    notebooksLoading,
    notebooksError,
    pushing,
    configured,
    load,
    loadNotebooks,
    save,
    test,
    push,
  }
})
