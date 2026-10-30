import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/errors'

const api = vi.hoisted(() => ({
  listTermSets: vi.fn(),
  getTermSet: vi.fn(),
  updateTermSet: vi.fn(async (id: string, body: object) => ({ id, ...body })),
  createTermSet: vi.fn(async (body: object) => ({ id: 'new', ...body })),
  deleteTermSet: vi.fn(async () => undefined),
  importTermSet: vi.fn(async (name: string) => ({ id: 'imp', name, terms: [{ term: 'A' }] })),
}))

vi.mock('@/api', async () => {
  const errors = await import('@/api/errors')
  return { ...api, toApiError: errors.toApiError }
})

const { useTermSetsStore } = await import('./termSets')

beforeEach(() => {
  setActivePinia(createPinia())
  api.listTermSets.mockResolvedValue([{ id: 't1', name: 'Set', terms: [{ term: 'Vue' }] }])
})

describe('term sets store', () => {
  it('addAlias re-reads the set so terms added elsewhere are kept', async () => {
    const store = useTermSetsStore()
    await store.load()
    // Meanwhile another tab added "Pinia" to the set.
    api.getTermSet.mockResolvedValue({
      id: 't1',
      name: 'Set',
      terms: [{ term: 'Vue' }, { term: 'Pinia' }],
    })
    await store.addAlias('t1', 'Vue', '有', 'unused')
    const body = api.updateTermSet.mock.calls[0]![1] as { terms: { term: string; aliases?: string[] }[] }
    expect(body.terms.map((t) => t.term)).toEqual(['Vue', 'Pinia'])
    expect(body.terms[0]!.aliases).toContain('有')
    expect(store.items[0]!.terms!.map((t) => t.term)).toEqual(['Vue', 'Pinia'])
  })

  it('addAlias without a set creates one', async () => {
    const store = useTermSetsStore()
    await store.load()
    const saved = await store.addAlias(null, 'Vue', 'view', '校对别名')
    expect(saved.name).toBe('校对别名')
    expect(store.items.map((s) => s.id)).toEqual(['t1', 'new'])
  })

  it('create / save / remove / import keep the cached list in step', async () => {
    const store = useTermSetsStore()
    await store.load()
    await store.create({ name: 'B', terms: [] })
    await store.save('t1', { name: 'Renamed', terms: [] })
    expect(store.items.map((s) => s.name)).toEqual(['Renamed', 'B'])
    await store.importText('Imported', 'A')
    expect(store.items.map((s) => s.id)).toEqual(['t1', 'new', 'imp'])
    await store.remove('new')
    expect(store.items.map((s) => s.id)).toEqual(['t1', 'imp'])
  })

  it('a 404 drops the set: on save (rethrown) and on delete (counts as deleted)', async () => {
    const store = useTermSetsStore()
    await store.load()
    api.updateTermSet.mockRejectedValueOnce(new ApiError(404, 'term set not found'))
    await expect(store.save('t1', { name: 'x', terms: [] })).rejects.toMatchObject({ status: 404 })
    expect(store.items).toEqual([])
    await store.load()
    api.deleteTermSet.mockRejectedValueOnce(new ApiError(404, 'gone'))
    await store.remove('t1')
    expect(store.items).toEqual([])
    api.deleteTermSet.mockRejectedValueOnce(new ApiError(500, 'boom'))
    await store.load()
    await expect(store.remove('t1')).rejects.toMatchObject({ status: 500 })
    expect(store.items).toHaveLength(1)
  })
})
