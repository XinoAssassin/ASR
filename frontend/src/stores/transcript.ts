/**
 * Transcript page: the transcript, its job (for the upload id) and the audio Blob for the
 * waveform. Audio is fetched with the X-API-Key header (D6), never via a URL with the key.
 *
 * Saving model:
 * - Text edits are shown at once and kept in `pendingText` until they are sent (one PATCH for all
 *   pending edits, AUTOSAVE_MS after the last keystroke, or before any other op / on flush()).
 * - Every write (text edits, ops, speakers, revert) runs on one "edit chain", so the server sees
 *   them in the order they were made. LLM suggestions / summaries run on a separate chain so that
 *   flush() (route leave, export) never waits minutes for them.
 * - Each piece of work captures its transcript id when it is queued. Work for a transcript that is
 *   no longer loaded still completes on the server, but never touches the store.
 * - Writes carry `expected_updated_at` (the last version this tab knows); a 409 means the
 *   transcript changed elsewhere: it is reloaded with the unsaved text re-applied, and the
 *   ApiError(409) is surfaced through `saveError` and the rejected promise.
 * - A failed save puts its text edits back into `pendingText` (unless newer text exists), keeps
 *   `saveState = 'error'`, and is retried on the next edit / flush() / retrySave().
 */
import { defineStore } from 'pinia'
import { computed, reactive, ref, watch } from 'vue'

import {
  ApiError,
  audioPath,
  createSummary,
  deleteSummary,
  downloadExport,
  fetchBlob,
  getJob,
  getTranscript,
  isAbortError,
  patchTranscript,
  requestLlmSuggestions,
  revertTranscript,
  toApiError,
  updateSpeakers,
  type ExportOptions,
  type JobOut,
  type LlmSuggestionsOut,
  type Speaker,
  type SpeakerUpdate,
  type TranscriptOp,
  type TranscriptOut,
} from '@/api'

/** Text edits are saved this long after the last keystroke (one PATCH for all pending edits). */
export const AUTOSAVE_MS = 600

export type SaveState = 'idle' | 'saving' | 'saved' | 'error'

/** Used when a speaker id is missing from `Transcript.speakers` (should not happen). */
const FALLBACK_COLORS = ['#5B8FF9', '#F6BD16', '#5AD8A6', '#E8684A', '#6DC8EC', '#9270CA']

interface TakenEdit {
  segmentId: string
  text: string
  /** Edit sequence number: a failed edit is restored only if no newer edit of it exists. */
  seq: number
}

interface WriteOptions {
  keepalive?: boolean
}

/** Whether ISO timestamp `a` is strictly older than `b` (µs-precise for same-format strings). */
function isOlder(a: string, b: string): boolean {
  const ta = Date.parse(a)
  const tb = Date.parse(b)
  if (Number.isFinite(ta) && Number.isFinite(tb) && ta !== tb) return ta < tb
  return a.length === b.length ? a < b : false
}

function hasBrowserEvents(): boolean {
  return typeof window !== 'undefined' && typeof window.addEventListener === 'function'
}

function bump(runs: Map<string, number>, id: string, by: number): void {
  const n = (runs.get(id) ?? 0) + by
  if (n > 0) runs.set(id, n)
  else runs.delete(id)
}

export const useTranscriptStore = defineStore('transcript', () => {
  const transcript = ref<TranscriptOut | null>(null)
  const job = ref<JobOut | null>(null)
  const loading = ref(false)
  const error = ref<ApiError | null>(null)

  const audio = ref<Blob | null>(null)
  const audioLoading = ref(false)
  const audioError = ref<ApiError | null>(null)
  const exporting = ref(false)
  let audioController: AbortController | null = null

  const saveState = ref<SaveState>('idle')
  const saveError = ref<ApiError | null>(null)
  /** Unsent text of the loaded transcript, by segment id. */
  const pendingText = reactive(new Map<string, string>())
  /** Latest edit sequence number per segment (pending or sent). */
  const latestSeq = new Map<string, number>()
  let editSeq = 0
  /** Writes queued or in flight, for any transcript. */
  const activeWrites = ref(0)
  /** The same, per transcript id. */
  const writesFor = new Map<string, number>()
  /** Latest `updated_at` this tab knows per transcript id: sent as `expected_updated_at`. */
  const versions = new Map<string, string>()
  /** Bumped per transcript id on a 409: writes queued before it are based on a stale view. */
  const conflicts = new Map<string, number>()
  /** LLM / summary runs in progress per transcript id. */
  const llmRuns = reactive(new Map<string, number>())
  const summaryRuns = reactive(new Map<string, number>())

  let autosaveTimer: ReturnType<typeof setTimeout> | null = null
  let editChain: Promise<unknown> = Promise.resolve()
  let slowChain: Promise<unknown> = Promise.resolve()
  let loadSeq = 0
  let loadController: AbortController | null = null

  /** Term names of the job (highlighted while proofreading). */
  const terms = computed(() => (job.value?.options.terms ?? []).map((t) => t.term))

  const llmRunning = computed(() => !!transcript.value && llmRuns.has(transcript.value.id))
  const summaryRunning = computed(() => !!transcript.value && summaryRuns.has(transcript.value.id))

  /** Something typed is not on the server yet (or a save failed): warn before leaving. */
  // Failed text edits go back into pendingText, so an error state with nothing pending (a failed
  // merge, a 409 reload) has no local changes to lose.
  const hasUnsavedChanges = computed(() => pendingText.size > 0 || activeWrites.value > 0)

  const speakerMap = computed(() => {
    const map = new Map<string, Speaker>()
    for (const s of transcript.value?.speakers ?? []) map.set(s.id, s)
    let extra = 0
    for (const seg of transcript.value?.segments ?? []) {
      if (!map.has(seg.speaker_id)) {
        map.set(seg.speaker_id, {
          id: seg.speaker_id,
          name: seg.speaker_id,
          color: FALLBACK_COLORS[extra++ % FALLBACK_COLORS.length]!,
        })
      }
    }
    return map
  })

  // -- page lifecycle guards ---------------------------------------------------------------------

  function onBeforeUnload(e: BeforeUnloadEvent): void {
    e.preventDefault()
    e.returnValue = '' // older browsers need a value to show the prompt
  }

  /** Best effort: send what is pending with `keepalive`, so it survives the page going away. */
  function onPageHide(): void {
    if (pendingText.size) void submit([], { keepalive: true }).catch(() => undefined)
  }

  let pageHideListening = false
  function listenPageHide(on: boolean): void {
    if (!hasBrowserEvents() || on === pageHideListening) return
    pageHideListening = on
    if (on) window.addEventListener('pagehide', onPageHide)
    else window.removeEventListener('pagehide', onPageHide)
  }

  watch(
    hasUnsavedChanges,
    (unsaved) => {
      if (!hasBrowserEvents()) return
      if (unsaved) window.addEventListener('beforeunload', onBeforeUnload)
      else window.removeEventListener('beforeunload', onBeforeUnload)
    },
    { flush: 'sync' },
  )

  // -- server state ------------------------------------------------------------------------------

  function isCurrent(id: string): boolean {
    return transcript.value?.id === id
  }

  function noteVersion(id: string, updatedAt: string, authoritative = false): void {
    const known = versions.get(id)
    if (authoritative || !known || !isOlder(updatedAt, known)) versions.set(id, updatedAt)
  }

  /** The transcript with the unsent text on top (pending edits of vanished segments dropped). */
  function withPending(t: TranscriptOut): TranscriptOut {
    if (!pendingText.size) return t
    const ids = new Set(t.segments.map((s) => s.id))
    for (const segmentId of [...pendingText.keys()]) {
      if (!ids.has(segmentId)) pendingText.delete(segmentId)
    }
    return {
      ...t,
      segments: t.segments.map((s) => {
        const text = pendingText.get(s.id)
        return text === undefined || text === s.text ? s : { ...s, text }
      }),
    }
  }

  /** A server answer for transcript `id`: shown only if it is still loaded and not older. */
  function accept(id: string, updated: TranscriptOut, authoritative = false): void {
    noteVersion(id, updated.updated_at, authoritative)
    const current = transcript.value
    if (!current || current.id !== id) return
    if (!authoritative && isOlder(updated.updated_at, current.updated_at)) return
    transcript.value = withPending(updated)
  }

  async function loadAudio(uploadId: string): Promise<void> {
    audioController?.abort()
    const controller = new AbortController()
    audioController = controller
    audioLoading.value = true
    audioError.value = null
    try {
      const blob = (await fetchBlob(audioPath(uploadId), controller.signal)).blob
      if (audioController === controller) audio.value = blob
    } catch (e) {
      if (!isAbortError(e) && audioController === controller) audioError.value = toApiError(e)
    } finally {
      if (audioController === controller) {
        audioLoading.value = false
        audioController = null
      }
    }
  }

  /**
   * Show transcript `transcriptId`. Text typed on the transcript shown so far is sent first, on
   * that transcript's id (failures are not fatal here: await flush() in the route guard to handle
   * them). Late answers of an earlier load() are ignored. Use this both for the first load and
   * for A → B switches.
   */
  async function load(transcriptId: string): Promise<void> {
    const seq = ++loadSeq
    loadController?.abort()
    loadController = null
    loading.value = true
    error.value = null
    // Pending text is taken into a request right here; only a reload of a transcript with
    // writes in flight waits for them (another transcript does not depend on them).
    const saved = flush().catch(() => undefined)
    if (writesFor.has(transcriptId)) await saved
    else await Promise.resolve()
    if (seq !== loadSeq) return
    drainPending() // typed while the flush was running
    clearState()
    const controller = new AbortController()
    loadController = controller
    loading.value = true
    try {
      const t = await getTranscript(transcriptId, controller.signal)
      if (seq !== loadSeq) return
      noteVersion(t.id, t.updated_at, true)
      transcript.value = t
      listenPageHide(true)
      try {
        const j = await getJob(t.job_id, controller.signal)
        if (seq !== loadSeq) return
        job.value = j
      } catch (e) {
        if (seq !== loadSeq || isAbortError(e)) return
        audioError.value = toApiError(e)
      }
    } catch (e) {
      if (seq !== loadSeq || isAbortError(e)) return
      error.value = toApiError(e)
    } finally {
      if (seq === loadSeq) {
        loading.value = false
        loadController = null
      }
    }
    if (seq !== loadSeq) return
    const uploadId = job.value?.upload_id
    if (uploadId) void loadAudio(uploadId)
  }

  // -- writes ------------------------------------------------------------------------------------

  function clearTimer(): void {
    if (autosaveTimer !== null) clearTimeout(autosaveTimer)
    autosaveTimer = null
  }

  /** Move the pending edits of segments that still exist into a request (synchronously). */
  function takePendingEdits(): TakenEdit[] {
    const known = new Set((transcript.value?.segments ?? []).map((s) => s.id))
    const edits: TakenEdit[] = []
    for (const [segmentId, text] of pendingText) {
      if (known.has(segmentId)) edits.push({ segmentId, text, seq: latestSeq.get(segmentId) ?? 0 })
    }
    pendingText.clear()
    return edits
  }

  /** Put failed edits back unless the segment was edited again since (or reverted). */
  function restoreEdits(edits: TakenEdit[]): void {
    for (const e of edits) {
      if (latestSeq.get(e.segmentId) === e.seq && !pendingText.has(e.segmentId)) {
        pendingText.set(e.segmentId, e.text)
      }
    }
  }

  /** After a 409: show the server's version, with the unsent text re-applied. */
  async function reloadAfterConflict(id: string): Promise<void> {
    try {
      const fresh = await getTranscript(id)
      accept(id, fresh, true)
    } catch {
      // Keep what is on screen; the next save gets the 409 again and retries the reload.
    }
  }

  /**
   * Queue a write for transcript `id` on the edit chain. `expected` is read when the write runs,
   * so it includes this tab's own earlier writes.
   */
  function enqueueWrite(
    id: string,
    call: (expected: string | null) => Promise<TranscriptOut>,
    edits: TakenEdit[] = [],
  ): Promise<void> {
    activeWrites.value++
    bump(writesFor, id, 1)
    if (isCurrent(id)) saveState.value = 'saving'
    const epoch = conflicts.get(id) ?? 0
    const work = async (): Promise<void> => {
      let ok = false
      try {
        // Queued before a 409 reload: do not apply it to a version the user has not seen.
        if ((conflicts.get(id) ?? 0) !== epoch) throw new ApiError(409, 'transcript changed elsewhere')
        const updated = await call(versions.get(id) ?? null)
        accept(id, updated)
        ok = true
      } catch (e) {
        const err = toApiError(e)
        if (isCurrent(id)) {
          restoreEdits(edits)
          saveState.value = 'error'
          saveError.value = err
          if (err.status === 409 && (conflicts.get(id) ?? 0) === epoch) {
            conflicts.set(id, epoch + 1)
            await reloadAfterConflict(id)
          }
        }
        throw err
      } finally {
        activeWrites.value--
        bump(writesFor, id, -1)
        if (ok && isCurrent(id) && !pendingText.size && activeWrites.value === 0) {
          saveState.value = 'saved'
          saveError.value = null
        }
      }
    }
    const run = editChain.then(work, work)
    editChain = run.catch(() => undefined)
    return run
  }

  /** Send the pending text edits plus `ops` (in that order) in one PATCH. */
  function submit(ops: TranscriptOp[], options: WriteOptions = {}): Promise<void> {
    clearTimer()
    const id = transcript.value?.id
    if (!id) return Promise.resolve()
    const edits = takePendingEdits()
    const all: TranscriptOp[] = [
      ...edits.map((e): TranscriptOp => ({ op: 'edit_text', segment_id: e.segmentId, text: e.text })),
      ...ops,
    ]
    if (!all.length) return Promise.resolve()
    return enqueueWrite(
      id,
      (expected) =>
        patchTranscript(id, all, { expectedUpdatedAt: expected, keepalive: options.keepalive }),
      edits,
    )
  }

  /** Apply ops after any pending text edits (which are sent first, in the same request). */
  function applyOps(ops: TranscriptOp[]): Promise<void> {
    return submit(ops)
  }

  /** Inline text edit: shown immediately, saved AUTOSAVE_MS after the last change. */
  function editText(segmentId: string, text: string): void {
    const seg = transcript.value?.segments.find((s) => s.id === segmentId)
    if (!seg) return
    // Compare with what will be sent, not with seg.text (which may be a newer server answer).
    if ((pendingText.get(segmentId) ?? seg.text) === text && seg.text === text) return
    seg.text = text
    pendingText.set(segmentId, text)
    latestSeq.set(segmentId, ++editSeq)
    if (saveState.value !== 'error') saveState.value = 'saving'
    clearTimer()
    autosaveTimer = setTimeout(() => void flush().catch(() => undefined), AUTOSAVE_MS)
  }

  /**
   * Send pending text now and wait for every queued write (text, ops, speakers, revert) — but
   * not for LLM suggestions / summaries. Rejects when sending the pending text fails; check
   * `hasUnsavedChanges` afterwards (e.g. in a route guard).
   */
  async function flush(): Promise<void> {
    clearTimer()
    if (pendingText.size && transcript.value) await submit([])
    else await editChain
  }

  /** Retry after a failed save (the failed text edits are pending again). */
  function retrySave(): Promise<void> {
    if (!pendingText.size && saveState.value === 'error' && activeWrites.value === 0) {
      saveState.value = 'idle'
      saveError.value = null
    }
    return flush()
  }

  function saveSpeakers(speakers: SpeakerUpdate[]): Promise<void> {
    const id = transcript.value?.id
    if (!id) return Promise.resolve()
    return enqueueWrite(id, (expected) =>
      updateSpeakers(id, speakers, { expectedUpdatedAt: expected }),
    )
  }

  /** New speaker id that is not taken yet (SPEAKER_nn). */
  function nextSpeakerId(): string {
    const taken = new Set((transcript.value?.speakers ?? []).map((s) => s.id))
    let n = taken.size
    while (taken.has(`SPEAKER_${String(n).padStart(2, '0')}`)) n++
    return `SPEAKER_${String(n).padStart(2, '0')}`
  }

  function revert(): Promise<void> {
    const id = transcript.value?.id
    if (!id) return Promise.resolve()
    clearTimer()
    pendingText.clear()
    latestSeq.clear() // edits in flight that fail are not restored over the reverted text
    return enqueueWrite(id, () => revertTranscript(id), [])
  }

  // -- slow LLM work -----------------------------------------------------------------------------

  function enqueueSlow<T>(work: () => Promise<T>): Promise<T> {
    const run = slowChain.then(work, work)
    slowChain = run.catch(() => undefined)
    return run
  }

  function currentId(): string {
    const id = transcript.value?.id
    if (!id) throw new Error('No transcript')
    return id
  }

  async function suggestWithLlm(segmentIds: string[] | null): Promise<LlmSuggestionsOut> {
    const id = currentId()
    bump(llmRuns, id, 1)
    try {
      await flush() // the LLM checks what is on screen
      return await enqueueSlow(async () => {
        const result = await requestLlmSuggestions(id, segmentIds)
        accept(id, result.transcript)
        return result
      })
    } finally {
      bump(llmRuns, id, -1)
    }
  }

  /** Generate (or regenerate) the LLM summary of what is on screen now. */
  async function summarize(): Promise<void> {
    const id = currentId()
    bump(summaryRuns, id, 1)
    try {
      await flush()
      await enqueueSlow(async () => {
        const summary = await createSummary(id)
        if (transcript.value && isCurrent(id)) transcript.value = { ...transcript.value, summary }
      })
    } finally {
      bump(summaryRuns, id, -1)
    }
  }

  async function removeSummary(): Promise<void> {
    const id = currentId()
    await enqueueSlow(async () => {
      await deleteSummary(id)
      if (transcript.value && isCurrent(id)) transcript.value = { ...transcript.value, summary: null }
    })
  }

  async function exportAs(options: ExportOptions): Promise<string> {
    const id = currentId()
    await flush() // export what the user sees
    exporting.value = true
    try {
      return await downloadExport(id, options)
    } finally {
      exporting.value = false
    }
  }

  // -- lifecycle ---------------------------------------------------------------------------------

  function clearState(): void {
    clearTimer()
    pendingText.clear()
    latestSeq.clear()
    saveState.value = 'idle'
    saveError.value = null
    audioController?.abort()
    audioController = null
    listenPageHide(false)
    transcript.value = null
    job.value = null
    error.value = null
    audio.value = null
    audioError.value = null
    audioLoading.value = false
    exporting.value = false
  }

  /** Send what is pending (on its own transcript id) without waiting. */
  function drainPending(): void {
    if (pendingText.size && transcript.value) void submit([]).catch(() => undefined)
  }

  /**
   * Leave the transcript (view unmount). Pending text is still sent, on its own transcript id;
   * in-flight loads and the audio download are aborted, and late answers are ignored.
   */
  function reset(): void {
    loadSeq++
    loadController?.abort()
    loadController = null
    drainPending()
    clearState()
    loading.value = false
  }

  return {
    transcript,
    job,
    loading,
    error,
    audio,
    audioLoading,
    audioError,
    exporting,
    speakerMap,
    terms,
    saveState,
    saveError,
    hasUnsavedChanges,
    llmRunning,
    summaryRunning,
    load,
    exportAs,
    reset,
    applyOps,
    editText,
    flush,
    retrySave,
    saveSpeakers,
    nextSpeakerId,
    revert,
    suggestWithLlm,
    summarize,
    removeSummary,
  }
})
