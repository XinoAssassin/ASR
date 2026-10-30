import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  listJobs: vi.fn(),
  getUpload: vi.fn(async () => null),
}))

vi.mock('@/api', () => ({
  ...api,
  isTerminal: (s: string) => ['succeeded', 'failed', 'cancelled'].includes(s),
  toApiError: (e: unknown) => e,
  watchJob: vi.fn(),
}))

const { useJobsStore } = await import('./jobs')

beforeEach(() => setActivePinia(createPinia()))

describe('jobs store', () => {
  it('ignores a list response that answers after a newer request', async () => {
    let resolvePage1!: (v: unknown) => void
    api.listJobs.mockImplementationOnce(() => new Promise((r) => (resolvePage1 = r)))
    api.listJobs.mockImplementationOnce(async () => ({
      items: [{ id: 'p2', status: 'succeeded', upload_id: null }],
      total: 30,
    }))
    const store = useJobsStore()
    const first = store.setPage(1)
    await store.setPage(2)
    expect(store.loading).toBe(false)
    resolvePage1({ items: [{ id: 'p1', status: 'succeeded', upload_id: null }], total: 30 })
    await first
    expect(store.items.map((j) => j.id)).toEqual(['p2'])
    expect(store.page).toBe(2)
  })
})
