import { defineStore } from 'pinia'
import { ref } from 'vue'

import { listTermSets, toApiError, type ApiError, type TermSetOut } from '@/api'

export const useTermSetsStore = defineStore('termSets', () => {
  const items = ref<TermSetOut[]>([])
  const loading = ref(false)
  const error = ref<ApiError | null>(null)

  async function load(): Promise<void> {
    loading.value = true
    try {
      items.value = await listTermSets()
      error.value = null
    } catch (e) {
      error.value = toApiError(e)
    } finally {
      loading.value = false
    }
  }

  return { items, loading, error, load }
})
