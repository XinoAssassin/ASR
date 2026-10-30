import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { TranscriptOp, TranscriptOut } from '@/api'
import { ApiError } from '@/api/errors'

/** A promise settled from the test. */
interface Deferred<T> {
  promise: Promise<T>
  resolve: (v: T) => void
  reject: (e: unknown) => void
}
function deferred<T>(): Deferred<T> {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

const api = vi.hoisted(() => ({
  getTranscript: vi.fn(),
  getJob: vi.fn(),
  patchTranscript: vi.fn(),
  updateSpeakers: vi.fn(),
  revertTranscript: vi.fn(),
  requestLlmSuggestions: vi.fn(),
  createSummary: vi.fn(),
  deleteSummary: vi.fn(),
  fetchBlob: vi.fn(),
  downloadExport: vi.fn(),
}))

vi.mock('@/api', async () => {
  const errors = await import('@/api/errors')
  return {
    ...api,
    ApiError: errors.ApiError,
    toApiError: errors.toApiError,
    isAbortError: errors.isAbortError,
    audioPath: (id: string) => `/api/audio/${id}`,
  }
})

const { AUTOSAVE_MS, useTranscriptStore } = await import('./transcript')

let clock = 0
/** Server-side state per transcript id, mutated by the fake PATCH. */
const server = new Map<string, TranscriptOut>()

function stamp(): string {
  clock++
  return `2026-10-30T10:00:${String(clock).padStart(2, '0')}.000000Z`
}

function makeTranscript(id: string, texts: string[]): TranscriptOut {
  return {
    id,
    job_id: `job-${id}`,
    provider: 'cloud',
    duration: 10,
    created_at: '2026-10-30T09:00:00Z',
    updated_at: stamp(),
    speakers: [{ id: 'SPEAKER_00', name: 'A', color: '#000' }],
    segments: texts.map((text, i) => ({
      id: `s${i}`,
      start: i,
      end: i + 1,
      speaker_id: 'SPEAKER_00',
      text,
    })),
    original: [],
    corrections: [],
    summary: null,
  } as unknown as TranscriptOut
}

function applyOnServer(id: string, ops: TranscriptOp[]): TranscriptOut {
  const t = structuredClone(server.get(id)!)
  for (const op of ops) {
    if (op.op === 'edit_text') t.segments.find((s) => s.id === op.segment_id)!.text = op.text
  }
  t.updated_at = stamp()
  server.set(id, t)
  return structuredClone(t)
}

type PatchCall = { id: string; ops: TranscriptOp[]; expected: string | null | undefined }
const patches: PatchCall[] = []
/** updated_at of every successful fake PATCH answer. */
const answered: string[] = []

/** Default PATCH: honours expected_updated_at like the backend (409 on mismatch). */
async function fakePatch(
  id: string,
  ops: TranscriptOp[],
  options: { expectedUpdatedAt?: string | null } = {},
): Promise<TranscriptOut> {
  patches.push({ id, ops, expected: options.expectedUpdatedAt })
  const current = server.get(id)!
  if (options.expectedUpdatedAt && options.expectedUpdatedAt !== current.updated_at) {
    throw new ApiError(409, 'transcript was changed')
  }
  const out = applyOnServer(id, ops)
  answered.push(out.updated_at)
  return out
}

const texts = (ops: TranscriptOp[]) =>
  ops.map((o) => (o.op === 'edit_text' ? `${o.segment_id}=${o.text}` : o.op))

async function settle(): Promise<void> {
  for (let i = 0; i < 20; i++) await Promise.resolve()
}

beforeEach(() => {
  vi.useFakeTimers()
  clock = 0
  server.clear()
  patches.length = 0
  answered.length = 0
  for (const fn of Object.values(api)) fn.mockReset()
  server.set('A', makeTranscript('A', ['abc', 'second']))
  server.set('B', makeTranscript('B', ['bee']))
  api.getTranscript.mockImplementation(async (id: string) => structuredClone(server.get(id)!))
  api.getJob.mockImplementation(async (id: string) => ({ id, upload_id: null, options: { terms: [] } }))
  api.patchTranscript.mockImplementation(fakePatch)
  setActivePinia(createPinia())
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

async function loaded(id = 'A') {
  const store = useTranscriptStore()
  await store.load(id)
  return store
}

describe('transcript store: text edits', () => {
  it('sends the latest text when the user types back to an earlier value mid-save', async () => {
    const store = await loaded()
    const first = deferred<TranscriptOut>()
    api.patchTranscript.mockImplementationOnce(async (id: string, ops: TranscriptOp[], o) => {
      patches.push({ id, ops, expected: o?.expectedUpdatedAt })
      await first.promise
      return applyOnServer(id, ops)
    })

    store.editText('s0', 'abcd')
    await vi.advanceTimersByTimeAsync(AUTOSAVE_MS) // PATCH "abcd" in flight
    store.editText('s0', 'abc') // back to the original text while "abcd" is saving
    first.resolve(server.get('A')!)
    await settle()
    // The "abcd" answer must not hide the newer pending text...
    expect(store.transcript!.segments[0]!.text).toBe('abc')
    expect(store.saveState).toBe('saving')
    await store.flush()
    // ...and "abc" is sent, even though it equals the text before the first save.
    expect(patches.map((p) => texts(p.ops))).toEqual([['s0=abcd'], ['s0=abc']])
    expect(server.get('A')!.segments[0]!.text).toBe('abc')
    expect(store.saveState).toBe('saved')
    expect(store.hasUnsavedChanges).toBe(false)
  })

  it('sends expected_updated_at, including this tab\'s own earlier writes', async () => {
    const store = await loaded()
    const v0 = store.transcript!.updated_at
    store.editText('s0', 'one')
    const op = store.applyOps([{ op: 'merge', segment_id: 's1' } as TranscriptOp])
    store.editText('s1', 'x')
    await op
    await store.flush()
    expect(patches).toHaveLength(2)
    expect(patches[0]!.expected).toBe(v0)
    // The second PATCH was queued before the first answered, yet expects the first's answer.
    expect(patches[1]!.expected).toBe(answered[0])
    expect(store.transcript!.updated_at).toBe(answered[1])
  })

  it('restores failed edits, reports the error and retries on the next flush', async () => {
    const store = await loaded()
    api.patchTranscript.mockImplementationOnce(async () => {
      throw new ApiError(0, null)
    })
    store.editText('s0', 'typed')
    await expect(store.flush()).rejects.toMatchObject({ status: 0 })
    expect(store.saveState).toBe('error')
    expect(store.saveError?.status).toBe(0)
    expect(store.hasUnsavedChanges).toBe(true)
    expect(store.transcript!.segments[0]!.text).toBe('typed')

    await store.retrySave()
    expect(server.get('A')!.segments[0]!.text).toBe('typed')
    expect(store.saveState).toBe('saved')
    expect(store.saveError).toBeNull()
    expect(store.hasUnsavedChanges).toBe(false)
  })

  it('a failed op with no pending text leaves nothing unsaved', async () => {
    const store = await loaded()
    api.patchTranscript.mockImplementationOnce(async () => {
      throw new ApiError(500, 'boom')
    })
    await expect(store.applyOps([{ op: 'merge', segment_id: 's0' }])).rejects.toMatchObject({ status: 500 })
    expect(store.saveState).toBe('error')
    expect(store.hasUnsavedChanges).toBe(false)
  })

  it('does not restore a failed edit over newer text', async () => {
    const store = await loaded()
    const first = deferred<TranscriptOut>()
    api.patchTranscript.mockImplementationOnce(() => first.promise)
    store.editText('s0', 'old')
    const saving = store.flush()
    store.editText('s0', 'new')
    first.reject(new ApiError(503, null))
    await expect(saving).rejects.toBeInstanceOf(ApiError)
    expect(store.transcript!.segments[0]!.text).toBe('new')
    await store.flush()
    expect(server.get('A')!.segments[0]!.text).toBe('new')
  })

  it('on 409 reloads the transcript, re-applies unsaved text and surfaces the conflict', async () => {
    const store = await loaded()
    // Someone else edits segment 1 in another tab.
    applyOnServer('A', [{ op: 'edit_text', segment_id: 's1', text: 'elsewhere' }])
    store.editText('s0', 'mine')
    const err = await store.flush().catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(409)
    expect(store.saveError?.isConflict).toBe(true)
    expect(store.saveState).toBe('error')
    // Reloaded: the other tab's text is shown, and the unsaved text is still on top.
    expect(store.transcript!.segments.map((s) => s.text)).toEqual(['mine', 'elsewhere'])
    expect(store.transcript!.updated_at).toBe(server.get('A')!.updated_at)
    // The retry is based on the reloaded version and succeeds.
    await store.retrySave()
    expect(server.get('A')!.segments.map((s) => s.text)).toEqual(['mine', 'elsewhere'])
    expect(store.saveState).toBe('saved')
  })

  it('shows text restored from a write queued behind a 409 (screen = what a retry sends)', async () => {
    const store = await loaded()
    applyOnServer('A', [{ op: 'edit_text', segment_id: 's1', text: 'elsewhere' }])
    store.editText('s0', 'one')
    const first = store.flush() // PATCH 1: will get the 409
    store.editText('s1', 'two')
    const second = store.flush() // PATCH 2: queued behind it, fails after the reload
    await expect(first).rejects.toMatchObject({ status: 409 })
    await expect(second).rejects.toMatchObject({ status: 409 })
    expect(store.transcript!.segments.map((s) => s.text)).toEqual(['one', 'two'])
    expect(store.hasUnsavedChanges).toBe(true)
    await store.retrySave()
    expect(server.get('A')!.segments.map((s) => s.text)).toEqual(['one', 'two'])
    expect(store.saveState).toBe('saved')
  })

  it('drops ops queued before a 409 instead of applying them to the reloaded version', async () => {
    const store = await loaded()
    applyOnServer('A', [{ op: 'edit_text', segment_id: 's1', text: 'elsewhere' }])
    const a = store.applyOps([{ op: 'merge', segment_id: 's1' } as TranscriptOp])
    const b = store.applyOps([{ op: 'split', segment_id: 's0', at: 1 } as TranscriptOp])
    await expect(a).rejects.toMatchObject({ status: 409 })
    await expect(b).rejects.toMatchObject({ status: 409 })
    expect(patches).toHaveLength(1) // the split never reached the server
    expect(api.getTranscript).toHaveBeenCalledTimes(2) // load + one reload
  })
})

describe('transcript store: switching and leaving', () => {
  it('saves pending text of A on its own id when switching to B, and ignores late answers', async () => {
    const store = await loaded('A')
    const slow = deferred<TranscriptOut>()
    api.patchTranscript.mockImplementationOnce(async (id: string, ops: TranscriptOp[]) => {
      patches.push({ id, ops, expected: undefined })
      await slow.promise
      return applyOnServer(id, ops)
    })
    store.editText('s0', 'for A')
    await store.load('B') // does not wait for A's slow save
    expect(patches[0]).toMatchObject({ id: 'A' })
    expect(store.transcript!.id).toBe('B')
    expect(store.hasUnsavedChanges).toBe(true) // A's write is still in flight
    slow.resolve(server.get('A')!)
    await store.flush()
    expect(server.get('A')!.segments[0]!.text).toBe('for A')
    expect(store.transcript!.segments[0]!.text).toBe('bee')
    expect(store.saveState).toBe('idle')
    expect(store.hasUnsavedChanges).toBe(false)
  })

  it('reloading the same transcript waits for its writes first', async () => {
    const store = await loaded('A')
    store.editText('s0', 'reload me')
    await store.load('A')
    expect(store.transcript!.segments[0]!.text).toBe('reload me')
    expect(api.getTranscript).toHaveBeenCalledTimes(2)
  })

  it('reset() still sends pending text instead of dropping it', async () => {
    const store = await loaded('A')
    store.editText('s0', 'before unmount')
    store.reset()
    expect(store.transcript).toBeNull()
    expect(store.hasUnsavedChanges).toBe(true) // the write is in flight
    await store.flush()
    expect(server.get('A')!.segments[0]!.text).toBe('before unmount')
    expect(store.hasUnsavedChanges).toBe(false)
  })

  it('a write for A that answers after switching to B does not touch B', async () => {
    const store = await loaded('A')
    const slow = deferred<void>()
    api.updateSpeakers.mockImplementationOnce(async (id: string) => {
      await slow.promise
      const t = structuredClone(server.get(id)!)
      t.speakers = [{ id: 'SPEAKER_00', name: 'renamed', color: '#000' }] as TranscriptOut['speakers']
      t.updated_at = stamp()
      return t
    })
    const rename = store.saveSpeakers([{ id: 'SPEAKER_00', name: 'renamed' }])
    store.reset()
    const toB = store.load('B')
    slow.resolve()
    await rename
    await toB
    expect(store.transcript!.id).toBe('B')
    expect(store.transcript!.speakers[0]!.name).toBe('A')
    expect(store.saveState).toBe('idle')
  })

  it('ignores a stale load and aborts it', async () => {
    const store = useTranscriptStore()
    const slowA = deferred<TranscriptOut>()
    const signals: AbortSignal[] = []
    api.getTranscript.mockImplementationOnce((_id: string, signal: AbortSignal) => {
      signals.push(signal)
      return slowA.promise
    })
    const a = store.load('A')
    await settle()
    const b = store.load('B')
    await b
    slowA.resolve(structuredClone(server.get('A')!))
    await a
    expect(signals[0]!.aborted).toBe(true)
    expect(store.transcript!.id).toBe('B')
    expect(store.loading).toBe(false)
  })

  it('a load answering after reset() does not repopulate the store or fetch audio', async () => {
    const store = useTranscriptStore()
    const slow = deferred<TranscriptOut>()
    api.getTranscript.mockImplementationOnce(() => slow.promise)
    api.getJob.mockResolvedValue({ id: 'job-A', upload_id: 'u1', options: { terms: [] } })
    const a = store.load('A')
    await settle()
    store.reset()
    slow.resolve(structuredClone(server.get('A')!))
    await a
    expect(store.transcript).toBeNull()
    expect(store.loading).toBe(false)
    expect(api.fetchBlob).not.toHaveBeenCalled()
  })

  it('reset() aborts the audio download', async () => {
    api.getJob.mockResolvedValue({ id: 'job-A', upload_id: 'u1', options: { terms: [] } })
    let audioSignal: AbortSignal | undefined
    api.fetchBlob.mockImplementation((_p: string, options: { signal: AbortSignal }) => {
      audioSignal = options.signal
      return new Promise(() => undefined)
    })
    const store = await loaded('A')
    expect(store.audioLoading).toBe(true)
    store.reset()
    expect(audioSignal!.aborted).toBe(true)
    expect(store.audioLoading).toBe(false)
  })
})

describe('transcript store: audio', () => {
  it('reports download progress and drops the Blob once the player has it', async () => {
    api.getJob.mockResolvedValue({ id: 'job-A', upload_id: 'u1', options: { terms: [] } })
    const body = deferred<{ blob: Blob; filename: null }>()
    let progress: ((p: { loaded: number; total: number | null }) => void) | undefined
    api.fetchBlob.mockImplementation((_p: string, options: { onProgress: typeof progress }) => {
      progress = options.onProgress
      return body.promise
    })
    const store = await loaded('A')
    progress!({ loaded: 50, total: 200 })
    expect(store.audioProgress).toEqual({ loaded: 50, total: 200 })
    const blob = new Blob(['x'])
    body.resolve({ blob, filename: null })
    await settle()
    expect(store.audio).toBe(blob)
    expect(store.audioLoading).toBe(false)
    expect(store.audioProgress).toBeNull()
    store.releaseAudio(new Blob(['other'])) // a stale player cannot drop a newer download
    expect(store.audio).toBe(blob)
    store.releaseAudio(blob)
    expect(store.audio).toBeNull()
  })
})

describe('transcript store: LLM work', () => {
  it('flush() does not wait for a running LLM suggestion request', async () => {
    const store = await loaded('A')
    const llm = deferred<unknown>()
    api.requestLlmSuggestions.mockImplementation(() => llm.promise)
    const running = store.suggestWithLlm(null)
    await settle()
    expect(store.llmRunning).toBe(true)
    store.editText('s0', 'while llm runs')
    let flushed = false
    void store.flush().then(() => (flushed = true))
    await settle()
    expect(flushed).toBe(true)
    expect(server.get('A')!.segments[0]!.text).toBe('while llm runs')
    // The LLM answer (older than our edit) lands on its own transcript only.
    const t = structuredClone(server.get('A')!)
    llm.resolve({ transcript: t, added: 0, discarded: 0 })
    await running
    expect(store.llmRunning).toBe(false)
  })

  it('an LLM run started on A applies to A only, even if B is shown when it ends', async () => {
    const store = await loaded('A')
    const llm = deferred<unknown>()
    api.requestLlmSuggestions.mockImplementation(() => llm.promise)
    const running = store.suggestWithLlm(['s0'])
    await settle()
    await store.load('B')
    expect(store.llmRunning).toBe(false) // B has no run
    const fromA = structuredClone(server.get('A')!)
    fromA.updated_at = stamp()
    llm.resolve({ transcript: fromA, added: 1, discarded: 0 })
    await running
    expect(api.requestLlmSuggestions).toHaveBeenCalledWith('A', ['s0'])
    expect(store.transcript!.id).toBe('B')
  })

  it('summary runs against the transcript it was started on', async () => {
    const store = await loaded('A')
    const summary = deferred<unknown>()
    api.createSummary.mockImplementation(() => summary.promise)
    const running = store.summarize()
    await settle()
    expect(store.summaryRunning).toBe(true)
    await store.load('B')
    summary.resolve({ created_at: 'x', chunks: 1 })
    await running
    expect(api.createSummary).toHaveBeenCalledWith('A')
    expect(store.transcript!.summary).toBeNull()
  })
})

describe('transcript store: beforeunload guard', () => {
  it('is registered only while there are unsaved changes', async () => {
    const listeners = new Map<string, Set<unknown>>()
    vi.stubGlobal('window', {
      addEventListener: (type: string, fn: unknown) => {
        if (!listeners.has(type)) listeners.set(type, new Set())
        listeners.get(type)!.add(fn)
      },
      removeEventListener: (type: string, fn: unknown) => listeners.get(type)?.delete(fn),
    })
    const count = (type: string) => listeners.get(type)?.size ?? 0
    const store = await loaded('A')
    expect(count('beforeunload')).toBe(0)
    expect(count('pagehide')).toBe(1)
    store.editText('s0', 'x')
    expect(count('beforeunload')).toBe(1)
    await store.flush()
    expect(count('beforeunload')).toBe(0)
    store.reset()
    expect(count('pagehide')).toBe(0)
  })

  it('pagehide sends pending text with keepalive', async () => {
    const listeners = new Map<string, () => void>()
    vi.stubGlobal('window', {
      addEventListener: (type: string, fn: () => void) => listeners.set(type, fn),
      removeEventListener: (type: string) => listeners.delete(type),
    })
    const store = await loaded('A')
    store.editText('s0', 'closing')
    listeners.get('pagehide')!()
    await settle()
    expect(api.patchTranscript).toHaveBeenCalledWith(
      'A',
      [{ op: 'edit_text', segment_id: 's0', text: 'closing' }],
      expect.objectContaining({ keepalive: true }),
    )
  })
})
