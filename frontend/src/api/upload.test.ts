import { afterAll, beforeEach, describe, expect, it, vi } from 'vitest'

// --- fakes (installed before client.ts builds its typed client) ---------------------------------

const net = vi.hoisted(() => ({
  /** [method, path] of every request. */
  log: [] as [string, string][],
  /** Next answers for complete / parts, consumed in order; default 2xx. */
  completeAnswers: [] as number[],
  partAnswers: [] as number[],
  inits: 0,
}))

vi.stubGlobal('window', {
  localStorage: {
    getItem: (k: string) => (k === 'asr.backendUrl' ? 'http://backend.test' : null),
    setItem: () => undefined,
    removeItem: () => undefined,
  },
})

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

vi.stubGlobal('fetch', async (input: Request | string) => {
  const request = typeof input === 'string' ? new Request(input) : input
  const path = new URL(request.url).pathname
  net.log.push([request.method, path])
  if (path === '/api/uploads/init') {
    net.inits++
    return json(201, { session_id: `sess${net.inits}`, part_size: 4, total_parts: 3 })
  }
  if (path.endsWith('/complete')) {
    const status = net.completeAnswers.shift() ?? 201
    if (status === 0) throw new TypeError('Failed to fetch')
    if (status >= 400) return json(status, { detail: `complete ${status}` })
    return json(201, { id: 'up1', original_name: 'a.wav', path: '/x/a.wav' })
  }
  return json(404, { detail: 'nope' })
})

class FakeXhr {
  status = 0
  responseText = ''
  upload: { onprogress: ((ev: { loaded: number }) => void) | null } = { onprogress: null }
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  onabort: (() => void) | null = null
  private method = ''
  private url = ''
  open(method: string, url: string): void {
    this.method = method
    this.url = url
  }
  setRequestHeader(): void {}
  abort(): void {
    this.onabort?.()
  }
  send(): void {
    const path = new URL(this.url).pathname
    net.log.push([this.method, path])
    const status = net.partAnswers.shift() ?? 200
    queueMicrotask(() => {
      if (status === 0) return this.onerror?.()
      this.status = status
      this.responseText = JSON.stringify(status < 300 ? { n: 0, size: 4 } : { detail: 'x' })
      this.onload?.()
    })
  }
}
vi.stubGlobal('XMLHttpRequest', FakeXhr)

const { uploadFileChunked } = await import('./upload')
type UploadSession = import('./upload').UploadSession

afterAll(() => vi.unstubAllGlobals())

beforeEach(() => {
  net.log.length = 0
  net.completeAnswers.length = 0
  net.partAnswers.length = 0
  net.inits = 0
})

const file = () => new File(['0123456789'], 'a.wav') // 10 bytes → 3 parts of 4
const parts = () => net.log.filter(([m]) => m === 'PUT').map(([, p]) => p.split('/').pop())

describe('uploadFileChunked', () => {
  it('retries complete on network errors and 5xx', async () => {
    net.completeAnswers.push(0, 503)
    const out = await uploadFileChunked(file(), 'dir', { retryDelayMs: 0 })
    expect(out.id).toBe('up1')
    expect(net.log.filter(([, p]) => p.endsWith('/complete'))).toHaveLength(3)
  })

  it('does not retry complete on a client error', async () => {
    net.completeAnswers.push(422)
    await expect(uploadFileChunked(file(), 'dir', { retryDelayMs: 0 })).rejects.toMatchObject({
      status: 422,
    })
    expect(net.log.filter(([, p]) => p.endsWith('/complete'))).toHaveLength(1)
  })

  it('reports the session and resumes from the first missing part', async () => {
    let session: UploadSession | null = null
    net.partAnswers.push(200, 500) // part 1 fails (attempts: 1)
    await expect(
      uploadFileChunked(file(), 'dir', { attempts: 1, onSession: (s) => (session = s) }),
    ).rejects.toMatchObject({ status: 500 })
    expect(session).toMatchObject({ sessionId: 'sess1', nextPart: 1, totalParts: 3 })

    net.log.length = 0
    await uploadFileChunked(file(), 'dir', { resume: session, retryDelayMs: 0 })
    expect(net.inits).toBe(1) // no new session
    expect(parts()).toEqual(['1', '2'])
  })

  it('starts over when the server no longer knows the session', async () => {
    const stale: UploadSession = {
      sessionId: 'gone',
      partSize: 4,
      totalParts: 3,
      nextPart: 2,
      targetDir: 'dir',
      size: 10,
    }
    net.partAnswers.push(404)
    await uploadFileChunked(file(), 'dir', { resume: stale, retryDelayMs: 0 })
    expect(net.inits).toBe(1)
    expect(parts()).toEqual(['2', '0', '1', '2'])
  })

  it('ignores a session made for another target directory', async () => {
    const other: UploadSession = {
      sessionId: 'old',
      partSize: 4,
      totalParts: 3,
      nextPart: 2,
      targetDir: 'elsewhere',
      size: 10,
    }
    await uploadFileChunked(file(), 'dir', { resume: other, retryDelayMs: 0 })
    expect(net.inits).toBe(1)
    expect(parts()).toEqual(['0', '1', '2'])
  })
})
