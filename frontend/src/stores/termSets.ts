/**
 * Term sets (PLAN §6.2): the list used by the Upload page's picker, the corrections panel's
 * "add as alias" and the Term sets page (create / edit incl. rename / delete / import).
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'

import {
  createTermSet,
  deleteTermSet,
  getTermSet,
  importTermSet,
  listTermSets,
  toApiError,
  updateTermSet,
  type ApiError,
  type TermSetIn,
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

  /** Insert or replace `saved` in the cached list. */
  function upsert(saved: TermSetOut): TermSetOut {
    items.value = items.value.some((s) => s.id === saved.id)
      ? items.value.map((s) => (s.id === saved.id ? saved : s))
      : [...items.value, saved]
    return saved
  }

  /** Throws ApiError. */
  async function create(body: TermSetIn): Promise<TermSetOut> {
    return upsert(await createTermSet(body))
  }

  /** Replace the name and the whole term list. Throws ApiError (404: deleted elsewhere). */
  async function save(id: string, body: TermSetIn): Promise<TermSetOut> {
    try {
      return upsert(await updateTermSet(id, body))
    } catch (e) {
      const err = toApiError(e)
      if (err.status === 404) items.value = items.value.filter((s) => s.id !== id)
      throw err
    }
  }

  /** Throws ApiError; a 404 (already gone) counts as deleted. */
  async function remove(id: string): Promise<void> {
    try {
      await deleteTermSet(id)
    } catch (e) {
      if (toApiError(e).status !== 404) throw e
    }
    items.value = items.value.filter((s) => s.id !== id)
  }

  /** New set from pasted / file text. Throws TermImportError (per-line errors) or ApiError. */
  async function importText(name: string, text: string): Promise<TermSetOut> {
    return upsert(await importTermSet(name, text))
  }

  /** "Add as alias": `alias` (a mis-hearing) becomes an alias of `term` in the chosen set, or in
   * a new set named `newSetName` when `termSetId` is null. Returns the saved set. */
  async function addAlias(
    termSetId: string | null,
    term: string,
    alias: string,
    newSetName: string,
  ): Promise<TermSetOut> {
    if (termSetId === null) {
      return create({ name: newSetName, terms: withAlias([], term, alias) })
    }
    // Re-read the set right before the PUT: the cached list may be stale (edited elsewhere),
    // and the PUT replaces the whole term list. Throws ApiError(404) if it is gone.
    const set = await getTermSet(termSetId)
    return upsert(
      await updateTermSet(termSetId, {
        name: set.name,
        terms: withAlias(set.terms ?? [], term, alias),
      }),
    )
  }

  return { items, loading, error, load, create, save, remove, importText, addAlias }
})
