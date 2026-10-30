import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { UploadSession } from '@/api'

const api = vi.hoisted(() => ({
  uploadFileChunked: vi.fn(),
  registerUpload: vi.fn(),
}))

vi.mock('@/api', async () => {
  const errors = await import('@/api/errors')
  return {
    ...api,
    readStorage: () => null,
    writeStorage: () => undefined,
    isAbortError: errors.isAbortError,
    toApiError: errors.toApiError,
  }
})

const { useUploadsStore } = await import('./uploads')

const wav = (name = 'a.wav') => new File(['x'], name, { lastModified: 1 })

beforeEach(() => {
  api.uploadFileChunked.mockReset()
  setActivePinia(createPinia())
})

describe('uploads store', () => {
  it('counts cancelled items as pending and restarts exactly those', async () => {
    const store = useUploadsStore()
    store.targetDir = '/data'
    store.addFiles([wav('a.wav'), wav('b.wav')])
    store.queue[0]!.status = 'cancelled'
    store.queue[1]!.status = 'done'
    expect(store.pendingCount).toBe(1)
    api.uploadFileChunked.mockResolvedValue({ id: 'u1', probe: null })
    await store.uploadAll()
    expect(api.uploadFileChunked).toHaveBeenCalledTimes(1)
    expect(api.uploadFileChunked.mock.calls[0]![0].name).toBe('a.wav')
    expect(store.pendingCount).toBe(0)
  })

  it('keeps the session of a failed upload and resumes it on retry', async () => {
    const store = useUploadsStore()
    store.targetDir = '/data'
    store.addFiles([wav()])
    const session: UploadSession = {
      sessionId: 's1',
      partSize: 4,
      totalParts: 3,
      nextPart: 2,
      targetDir: '/data',
      size: 1,
    }
    api.uploadFileChunked.mockImplementationOnce(async (_f, _d, o) => {
      o.onSession(session)
      throw new Error('network')
    })
    await store.uploadAll()
    expect(store.queue[0]!.status).toBe('error')
    expect(store.queue[0]!.session).toEqual(session)

    api.uploadFileChunked.mockResolvedValueOnce({ id: 'u1', probe: null })
    await store.uploadAll()
    expect(api.uploadFileChunked.mock.calls[1]![2].resume).toEqual(session)
    expect(store.queue[0]!.status).toBe('done')
    expect(store.queue[0]!.session).toBeNull()
  })

  it('replaces a failed / cancelled copy when the same file is added again', () => {
    const store = useUploadsStore()
    store.addFiles([wav()])
    store.addFiles([wav()]) // still pending: duplicate
    expect(store.queue).toHaveLength(1)
    const oldKey = store.queue[0]!.key
    store.queue[0]!.status = 'error'
    store.addFiles([wav()])
    expect(store.queue).toHaveLength(1)
    expect(store.queue[0]!.key).not.toBe(oldKey)
    expect(store.queue[0]!.status).toBe('pending')
    store.queue[0]!.status = 'cancelled'
    store.addFiles([wav()])
    expect(store.queue).toHaveLength(1)
    expect(store.queue[0]!.status).toBe('pending')
  })
})
