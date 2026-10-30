import { defineStore } from 'pinia'
import { ref } from 'vue'

import {
  createTermSet,
  getTermSet,
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
      // Re-read the set right before the PUT: the cached list may be stale (edited elsewhere),
      // and the PUT replaces the whole term list. Throws ApiError(404) if it is gone.
      const set = await getTermSet(termSetId)
      saved = await updateTermSet(termSetId, {
        name: set.name,
        terms: withAlias(set.terms ?? [], term, alias),
      })
      items.value = items.value.some((s) => s.id === saved.id)
        ? items.value.map((s) => (s.id === saved.id ? saved : s))
        : [...items.value, saved]
    }
    return saved
  }

  return { items, loading, error, load, addAlias }
})
