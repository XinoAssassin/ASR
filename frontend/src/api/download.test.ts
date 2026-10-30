import { afterEach, describe, expect, it, vi } from 'vitest'

vi.stubGlobal('window', { localStorage: { getItem: () => null, setItem: () => undefined, removeItem: () => undefined } })
const { exportPath, fetchBlob } = await import('./download')

afterEach(() => vi.unstubAllGlobals())

function streamed(parts: string[], headers: Record<string, string>): Response {
  const enc = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      for (const p of parts) c.enqueue(enc.encode(p))
      c.close()
    },
  })
  return new Response(body, { status: 200, headers })
}

describe('fetchBlob', () => {
  it('reports progress while reading the body and keeps the content type', async () => {
    vi.stubGlobal('window', { localStorage: { getItem: () => null } })
    vi.stubGlobal('fetch', async () => streamed(['abc', 'defg'], { 'content-type': 'audio/mpeg', 'content-length': '7' }))
    const seen: Array<[number, number | null]> = []
    const { blob } = await fetchBlob('/api/audio/u1', { onProgress: (p) => seen.push([p.loaded, p.total]) })
    expect(seen).toEqual([
      [0, 7],
      [3, 7],
      [7, 7],
    ])
    expect(blob.size).toBe(7)
    expect(blob.type).toBe('audio/mpeg')
  })

  it('has no total without Content-Length and still accepts a bare signal', async () => {
    vi.stubGlobal('window', { localStorage: { getItem: () => null } })
    vi.stubGlobal('fetch', async () => streamed(['ab'], {}))
    const totals: Array<number | null> = []
    await fetchBlob('/x', { onProgress: (p) => totals.push(p.total) })
    expect(totals.every((t) => t === null)).toBe(true)
    const { blob } = await fetchBlob('/x', new AbortController().signal)
    expect(blob.size).toBe(2)
  })
})

describe('exportPath', () => {
  it('sends the locale for Markdown only', () => {
    const md = new URL(
      exportPath('t/1', { format: 'md', timestamps: false, mergeSameSpeaker: true, locale: 'en' }),
      'http://x',
    )
    expect(md.pathname).toBe('/api/transcripts/t%2F1/export')
    expect(Object.fromEntries(md.searchParams)).toEqual({
      format: 'md',
      timestamps: 'false',
      merge_same_speaker: 'true',
      locale: 'en',
    })
    const srt = new URL(
      exportPath('t1', { format: 'srt', timestamps: true, mergeSameSpeaker: false, locale: 'en' }),
      'http://x',
    )
    expect(srt.searchParams.has('locale')).toBe(false)
  })
})
