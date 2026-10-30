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

describe('jobs store: live updates', () => {
  it('polls a job whose stream failed for good instead of leaving it frozen', async () => {
    vi.useFakeTimers()
    try {
      const { watchJob } = await import('@/api')
      const watch = vi.mocked(watchJob)
      let onError: ((e: unknown) => void) | undefined
      watch.mockReset()
      watch.mockImplementation((_id, options) => {
        onError = options.onError as (e: unknown) => void
        return { stop: vi.fn(), done: new Promise<void>(() => undefined) }
      })
      api.listJobs.mockReset()
      api.listJobs.mockImplementation(async () => ({
        items: [{ id: 'j1', status: 'running', upload_id: null }],
        total: 1,
      }))
      const store = useJobsStore()
      await store.load()
      store.startLive()
      expect(watch).toHaveBeenCalledTimes(1)
      onError!(new Error('404')) // e.g. a proxy answering the SPA's HTML for the stream URL
      expect(api.listJobs).toHaveBeenCalledTimes(1)
      await vi.advanceTimersByTimeAsync(5000)
      expect(api.listJobs).toHaveBeenCalledTimes(2) // polled
      expect(watch).toHaveBeenCalledTimes(1) // the failing stream is not reopened on each poll
      api.listJobs.mockImplementation(async () => ({
        items: [{ id: 'j1', status: 'succeeded', upload_id: null }],
        total: 1,
      }))
      await vi.advanceTimersByTimeAsync(5000)
      expect(store.items[0]!.status).toBe('succeeded')
      await vi.advanceTimersByTimeAsync(15000)
      expect(api.listJobs).toHaveBeenCalledTimes(3) // terminal: polling stopped
      store.stopLive()
    } finally {
      vi.useRealTimers()
    }
  })
})
