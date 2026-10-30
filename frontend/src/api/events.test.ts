import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/** Browser globals used by client.ts and @microsoft/fetch-event-source (openWhenHidden: true). */
const stored = new Map<string, string>()
const fetchMock = vi.fn<(input: string, init: RequestInit) => Promise<Response>>()

function stubBrowser(): void {
  vi.stubGlobal('window', {
    localStorage: {
      getItem: (k: string) => stored.get(k) ?? null,
      setItem: (k: string, v: string) => stored.set(k, v),
      removeItem: (k: string) => stored.delete(k),
    },
    setTimeout: (fn: () => void, ms: number) => setTimeout(fn, ms),
    clearTimeout: (id: ReturnType<typeof setTimeout>) => clearTimeout(id),
    fetch: () => Promise.reject(new Error('window.fetch must not be used')),
  })
  vi.stubGlobal('document', { addEventListener: () => undefined, removeEventListener: () => undefined })
  vi.stubGlobal('fetch', fetchMock)
}

stubBrowser()
const { setApiKey } = await import('./client')
const { watchJob } = await import('./events')

const job = (status: string) => ({ id: 'j1', status })

function sse(events: Array<{ event?: string; data: unknown }>): Response {
  const body = events
    .map((e) => `${e.event ? `event: ${e.event}\n` : ''}data: ${JSON.stringify(e.data)}\n\n`)
    .join('')
  return new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream' } })
}

function status(code: number, type = 'application/json', body = '{"detail":"x"}'): Response {
  return new Response(body, { status: code, headers: { 'content-type': type } })
}

function keyOf(call: number): string | undefined {
  const headers = fetchMock.mock.calls[call]![1].headers as Record<string, string>
  return headers['X-API-Key']
}

beforeEach(() => {
  stored.clear()
  fetchMock.mockReset()
  stubBrowser()
})

afterEach(() => vi.unstubAllGlobals())

describe('watchJob', () => {
  it('retries a retryable status with the current key and stops after a terminal job', async () => {
    setApiKey('old')
    fetchMock.mockImplementationOnce(async () => {
      setApiKey('new') // changed in Settings while the stream retries
      return status(503)
    })
    fetchMock.mockImplementationOnce(async () =>
      sse([{ event: 'job', data: job('running') }, { event: 'ping', data: 1 }, { event: 'job', data: job('succeeded') }]),
    )
    const onJob = vi.fn()
    const onError = vi.fn()
    const stream = watchJob('j1', { onJob, onError, minRetryMs: 1 })
    await stream.done
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(fetchMock.mock.calls[0]![0]).toBe('/api/jobs/j1/events')
    expect(keyOf(0)).toBe('old')
    expect(keyOf(1)).toBe('new')
    expect(onJob.mock.calls.map((c) => c[0].status)).toEqual(['running', 'succeeded'])
    expect(onError).not.toHaveBeenCalled()
  })

  it('reconnects with back-off when the stream closes before a terminal status', async () => {
    fetchMock
      .mockImplementationOnce(async () => sse([{ event: 'job', data: job('running') }]))
      .mockImplementationOnce(async () => status(502))
      .mockImplementationOnce(async () => sse([{ event: 'job', data: job('failed') }]))
    const onJob = vi.fn()
    const stream = watchJob('j1', { onJob, minRetryMs: 1, maxRetryMs: 4 })
    await stream.done
    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(onJob.mock.calls.map((c) => c[0].status)).toEqual(['running', 'failed'])
  })

  it.each([
    ['404', () => status(404)],
    ['401', () => status(401)],
    ['200 with an HTML page (SPA fallback of a wrong backend URL)', () => status(200, 'text/html', '<html>')],
  ])('a fatal %s ends the stream through onError without retrying', async (_name, response) => {
    fetchMock.mockImplementation(async () => response())
    const onError = vi.fn()
    const stream = watchJob('j1', { onJob: vi.fn(), onError, minRetryMs: 1 })
    await stream.done
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(onError).toHaveBeenCalledTimes(1)
    expect(onError.mock.calls[0]![0].name).toBe('ApiError')
  })

  it('stop() settles done', async () => {
    fetchMock.mockImplementation(() => new Promise<Response>(() => undefined))
    const stream = watchJob('j1', { onJob: vi.fn() })
    stream.stop()
    await expect(stream.done).resolves.toBeUndefined()
  })
})
