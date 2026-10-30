import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  listTermSets: vi.fn(),
  getTermSet: vi.fn(),
  updateTermSet: vi.fn(async (id: string, body: object) => ({ id, ...body })),
  createTermSet: vi.fn(),
}))

vi.mock('@/api', () => ({ ...api, toApiError: (e: unknown) => e }))

const { useTermSetsStore } = await import('./termSets')

beforeEach(() => setActivePinia(createPinia()))

describe('term sets store', () => {
  it('addAlias re-reads the set so terms added elsewhere are kept', async () => {
    api.listTermSets.mockResolvedValue([{ id: 't1', name: 'Set', terms: [{ term: 'Vue' }] }])
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
})
