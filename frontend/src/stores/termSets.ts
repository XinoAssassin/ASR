import { defineStore } from 'pinia'
import { ref } from 'vue'

import {
  createTermSet,
  listTermSets,
  toApiError,
  updateTermSet,
  type ApiError,
  type TermSetOut,
} from '@/api'
import { withAlias } from '@/utils/search'

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

  /** "Add as alias": `alias` (a mis-hearing) becomes an alias of `term` in the chosen set, or in
   * a new set named `newSetName` when `termSetId` is null. Returns the saved set. */
  async function addAlias(
    termSetId: string | null,
    term: string,
    alias: string,
    newSetName: string,
  ): Promise<TermSetOut> {
    let saved: TermSetOut
    if (termSetId === null) {
      saved = await createTermSet({ name: newSetName, terms: withAlias([], term, alias) })
      items.value = [...items.value, saved]
    } else {
      const set = items.value.find((s) => s.id === termSetId)
      if (!set) throw new Error('term set not found')
      saved = await updateTermSet(termSetId, {
        name: set.name,
        terms: withAlias(set.terms ?? [], term, alias),
      })
      items.value = items.value.map((s) => (s.id === saved.id ? saved : s))
    }
    return saved
  }

  return { items, loading, error, load, addAlias }
})
