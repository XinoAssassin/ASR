import { defineStore } from 'pinia'
import { ref } from 'vue'

import { getHealth, toApiError, type HealthOut } from '@/api'

export type HealthState = 'idle' | 'checking' | 'connected' | 'error'

const POLL_MS = 30_000

export const useHealthStore = defineStore('health', () => {
  const state = ref<HealthState>('idle')
  const info = ref<HealthOut | null>(null)
  const error = ref<string | null>(null)
  /** Set when any request got 401; cleared when the API key changes or a check succeeds with auth. */
  const unauthorized = ref(false)
  let timer: ReturnType<typeof setInterval> | null = null

  async function check(): Promise<void> {
    if (state.value !== 'connected') state.value = 'checking'
    try {
      info.value = await getHealth()
      state.value = 'connected'
      error.value = null
    } catch (e) {
      info.value = null
      state.value = 'error'
      error.value = toApiError(e).message
    }
  }

  function startPolling(): void {
    if (timer) return
    void check()
    timer = setInterval(() => void check(), POLL_MS)
  }

  function stopPolling(): void {
    if (timer) clearInterval(timer)
    timer = null
  }

  function markUnauthorized(): void {
    unauthorized.value = true
  }

  function clearUnauthorized(): void {
    unauthorized.value = false
  }

  return {
    state,
    info,
    error,
    unauthorized,
    check,
    startPolling,
    stopPolling,
    markUnauthorized,
    clearUnauthorized,
  }
})
