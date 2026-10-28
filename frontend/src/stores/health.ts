import { defineStore } from 'pinia'
import { ref } from 'vue'

import { api, type Schemas } from '@/api/client'

export type HealthState = 'idle' | 'checking' | 'connected' | 'error'

export const useHealthStore = defineStore('health', () => {
  const state = ref<HealthState>('idle')
  const info = ref<Schemas['HealthOut'] | null>(null)
  const error = ref<string | null>(null)

  async function check(): Promise<void> {
    state.value = 'checking'
    error.value = null
    try {
      const result = await api.GET('/api/health')
      if (result.data) {
        info.value = result.data
        state.value = 'connected'
      } else {
        info.value = null
        state.value = 'error'
        error.value = `HTTP ${result.response.status}`
      }
    } catch (e) {
      info.value = null
      state.value = 'error'
      error.value = e instanceof Error ? e.message : String(e)
    }
  }

  return { state, info, error, check }
})
