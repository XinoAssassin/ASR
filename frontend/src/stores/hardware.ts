/**
 * Compute devices of the active backend (`GET /api/system/hardware`): which of cpu / cuda / rocm
 * the local pipeline can use there. `supported` is false for backends that predate the endpoint
 * (404): the UI then offers only the settings device.
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'

import { getHardware, toApiError, type ApiError, type HardwareOut } from '@/api'

export const useHardwareStore = defineStore('hardware', () => {
  const info = ref<HardwareOut | null>(null)
  const supported = ref(true)
  const loaded = ref(false)
  const loading = ref(false)
  const error = ref<ApiError | null>(null)
  let seq = 0

  async function load(): Promise<void> {
    const mine = ++seq
    loading.value = true
    try {
      const result = await getHardware()
      if (mine !== seq) return
      info.value = result
      supported.value = result !== null
      loaded.value = true
      error.value = null
    } catch (e) {
      if (mine !== seq) return
      error.value = toApiError(e)
    } finally {
      if (mine === seq) loading.value = false
    }
  }

  /** Another backend: forget everything (late answers of the old one are ignored). */
  function reset(): void {
    seq++
    info.value = null
    supported.value = true
    loaded.value = false
    loading.value = false
    error.value = null
  }

  return { info, supported, loaded, loading, error, load, reset }
})
