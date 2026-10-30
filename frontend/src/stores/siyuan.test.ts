import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/errors'

const api = vi.hoisted(() => ({
  getSiyuanSettings: vi.fn(),
  updateSiyuanSettings: vi.fn(),
  testSiyuan: vi.fn(),
  listSiyuanNotebooks: vi.fn(),
  pushToSiyuan: vi.fn(),
}))

vi.mock('@/api', async () => {
  const errors = await import('@/api/errors')
  return { ...api, toApiError: errors.toApiError }
})

const { useSiyuanStore } = await import('./siyuan')

const SETTINGS = {
  endpoint: 'http://127.0.0.1:6806',
  token_set: true,
  token_masked: '****abcd',
  notebook: '20260101000000-abcdefg',
  path_template: '/会议记录/{date} {title}',
  path_fields: ['title', 'date', 'time', 'source', 'provider'],
}
const BOOKS = [{ id: '20260101000000-abcdefg', name: 'Work', closed: false }]

const flush = () => new Promise((r) => setTimeout(r, 0))

beforeEach(() => {
  setActivePinia(createPinia())
  for (const fn of Object.values(api)) fn.mockReset()
})

describe('siyuan store', () => {
  it('loads the notebooks after a successful test only', async () => {
    const store = useSiyuanStore()
    api.testSiyuan.mockResolvedValueOnce({ ok: false, message: 'refused' })
    await store.test()
    expect(api.listSiyuanNotebooks).not.toHaveBeenCalled()
    api.testSiyuan.mockResolvedValueOnce({ ok: true, message: 'ok', version: '3.1', notebooks: 1 })
    api.listSiyuanNotebooks.mockResolvedValueOnce(BOOKS)
    await store.test()
    await flush()
    expect(store.testResult?.version).toBe('3.1')
    expect(store.notebooks).toEqual(BOOKS)
  })

  it('keeps notebook errors (422 not configured / 502 unreachable) for the UI', async () => {
    const store = useSiyuanStore()
    api.listSiyuanNotebooks.mockRejectedValueOnce(new ApiError(502, 'unreachable'))
    await store.loadNotebooks()
    expect(store.notebooks).toEqual([])
    expect(store.notebooksError?.status).toBe(502)
  })

  it('drops the notebooks and the test result when the token is cleared by an endpoint change', async () => {
    const store = useSiyuanStore()
    api.getSiyuanSettings.mockResolvedValueOnce(SETTINGS)
    await store.load()
    store.notebooks = BOOKS
    store.testResult = { ok: true, message: 'ok' }
    api.updateSiyuanSettings.mockResolvedValueOnce({
      ...SETTINGS,
      endpoint: 'http://host.docker.internal:6806',
      token_set: false,
      token_masked: null,
    })
    await store.save({ endpoint: 'http://host.docker.internal:6806' })
    expect(store.configured).toBe(false)
    expect(store.notebooks).toEqual([])
    expect(store.testResult).toBeNull()
    expect(api.listSiyuanNotebooks).not.toHaveBeenCalled()
  })

  it('reloads the notebooks after saving a new token', async () => {
    const store = useSiyuanStore()
    api.getSiyuanSettings.mockResolvedValueOnce({ ...SETTINGS, token_set: false, token_masked: null })
    await store.load()
    api.updateSiyuanSettings.mockResolvedValueOnce(SETTINGS)
    api.listSiyuanNotebooks.mockResolvedValueOnce(BOOKS)
    await store.save({ token: 'secret' })
    await flush()
    expect(api.updateSiyuanSettings).toHaveBeenCalledWith({ token: 'secret' })
    expect(store.notebooks).toEqual(BOOKS)
  })

  it('push passes the body through and tracks `pushing`', async () => {
    const store = useSiyuanStore()
    let resolve!: (v: unknown) => void
    api.pushToSiyuan.mockReturnValueOnce(new Promise((r) => (resolve = r)))
    const body = { notebook: BOOKS[0]!.id, path: null, locale: 'en' as const, timestamps: true, merge_same_speaker: false }
    const p = store.push('T1', body)
    expect(store.pushing).toBe(true)
    resolve({ doc_id: 'd', notebook: BOOKS[0]!.id, path: '/x', url: 'siyuan://blocks/d', warnings: [] })
    await p
    expect(store.pushing).toBe(false)
    expect(api.pushToSiyuan).toHaveBeenCalledWith('T1', body)
  })
})
