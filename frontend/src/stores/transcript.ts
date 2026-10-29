/**
 * Transcript page: the transcript, its job (for the upload id) and the audio Blob for the
 * waveform. Audio is fetched with the X-API-Key header (D6), never via a URL with the key.
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import {
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
  type ApiError,
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
  const llmRunning = ref(false)
  const summaryRunning = ref(false)
  const pendingText = new Map<string, string>()
  let autosaveTimer: ReturnType<typeof setTimeout> | null = null
  // Every mutation runs on this chain, so the server sees edits in the order they were made.
  let chain: Promise<unknown> = Promise.resolve()

  /** Term names of the job (highlighted while proofreading). */
  const terms = computed(() => (job.value?.options.terms ?? []).map((t) => t.term))

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

  async function loadAudio(uploadId: string): Promise<void> {
    audioController?.abort()
    const controller = new AbortController()
    audioController = controller
    audioLoading.value = true
    audioError.value = null
    try {
      audio.value = (await fetchBlob(audioPath(uploadId), controller.signal)).blob
    } catch (e) {
      if (!isAbortError(e)) audioError.value = toApiError(e)
    } finally {
      if (audioController === controller) {
        audioLoading.value = false
        audioController = null
      }
    }
  }

  async function load(transcriptId: string): Promise<void> {
    reset()
    loading.value = true
    try {
      transcript.value = await getTranscript(transcriptId)
      try {
        job.value = await getJob(transcript.value.job_id)
      } catch (e) {
        audioError.value = toApiError(e)
      }
    } catch (e) {
      error.value = toApiError(e)
    } finally {
      loading.value = false
    }
    const uploadId = job.value?.upload_id
    if (uploadId) void loadAudio(uploadId)
  }

  function enqueue<T>(work: () => Promise<T>): Promise<T> {
    const run = chain.then(work, work)
    chain = run.catch(() => undefined)
    return run
  }

  async function mutate(call: (id: string) => Promise<TranscriptOut>): Promise<void> {
    const current = transcript.value
    if (!current) return
    saveState.value = 'saving'
    try {
      const updated = await call(current.id)
      // A late answer for a transcript the user already left must not replace the new one.
      if (transcript.value?.id === current.id) transcript.value = updated
      saveState.value = 'saved'
      saveError.value = null
    } catch (e) {
      saveState.value = 'error'
      saveError.value = toApiError(e)
      throw saveError.value
    }
  }

  /** Apply ops after any pending text edits (which are sent first, in the same request). */
  function applyOps(ops: TranscriptOp[]): Promise<void> {
    if (autosaveTimer !== null) {
      clearTimeout(autosaveTimer)
      autosaveTimer = null
    }
    // Taken now (not when the chain gets here), so leaving the page cannot drop them.
    const all = [...takePendingEdits(), ...ops]
    return enqueue(() => {
      if (!all.length) return Promise.resolve()
      return mutate((id) => patchTranscript(id, all))
    })
  }

  function takePendingEdits(): TranscriptOp[] {
    const known = new Set((transcript.value?.segments ?? []).map((s) => s.id))
    const edits: TranscriptOp[] = []
    for (const [segmentId, text] of pendingText) {
      if (known.has(segmentId)) edits.push({ op: 'edit_text', segment_id: segmentId, text })
    }
    pendingText.clear()
    return edits
  }

  /** Inline text edit: shown immediately, saved AUTOSAVE_MS after the last change. */
  function editText(segmentId: string, text: string): void {
    const seg = transcript.value?.segments.find((s) => s.id === segmentId)
    if (!seg || seg.text === text) return
    seg.text = text
    pendingText.set(segmentId, text)
    saveState.value = 'saving'
    if (autosaveTimer !== null) clearTimeout(autosaveTimer)
    autosaveTimer = setTimeout(() => void flush(), AUTOSAVE_MS)
  }

  function flush(): Promise<void> {
    if (autosaveTimer !== null) {
      clearTimeout(autosaveTimer)
      autosaveTimer = null
    }
    if (!pendingText.size) return chain.then(() => undefined)
    return applyOps([])
  }

  function saveSpeakers(speakers: SpeakerUpdate[]): Promise<void> {
    return enqueue(() => mutate((id) => updateSpeakers(id, speakers)))
  }

  /** New speaker id that is not taken yet (SPEAKER_nn). */
  function nextSpeakerId(): string {
    const taken = new Set((transcript.value?.speakers ?? []).map((s) => s.id))
    let n = taken.size
    while (taken.has(`SPEAKER_${String(n).padStart(2, '0')}`)) n++
    return `SPEAKER_${String(n).padStart(2, '0')}`
  }

  function revert(): Promise<void> {
    pendingText.clear()
    return enqueue(() => mutate((id) => revertTranscript(id)))
  }

  async function suggestWithLlm(segmentIds: string[] | null): Promise<LlmSuggestionsOut> {
    await flush()
    llmRunning.value = true
    try {
      return await enqueue(async () => {
        const id = transcript.value!.id
        const result = await requestLlmSuggestions(id, segmentIds)
        if (transcript.value?.id === id) transcript.value = result.transcript
        return result
      })
    } finally {
      llmRunning.value = false
    }
  }

  /** Generate (or regenerate) the LLM summary of what is on screen now. */
  async function summarize(): Promise<void> {
    await flush()
    summaryRunning.value = true
    try {
      await enqueue(async () => {
        const id = transcript.value!.id
        const summary = await createSummary(id)
        if (transcript.value?.id === id) transcript.value = { ...transcript.value, summary }
      })
    } finally {
      summaryRunning.value = false
    }
  }

  async function removeSummary(): Promise<void> {
    await enqueue(async () => {
      const id = transcript.value!.id
      await deleteSummary(id)
      if (transcript.value?.id === id) transcript.value = { ...transcript.value, summary: null }
    })
  }

  async function exportAs(options: ExportOptions): Promise<string> {
    if (!transcript.value) throw new Error('No transcript')
    await flush() // export what the user sees
    exporting.value = true
    try {
      return await downloadExport(transcript.value.id, options)
    } finally {
      exporting.value = false
    }
  }

  function reset(): void {
    if (autosaveTimer !== null) clearTimeout(autosaveTimer)
    autosaveTimer = null
    pendingText.clear()
    saveState.value = 'idle'
    saveError.value = null
    audioController?.abort()
    audioController = null
    transcript.value = null
    job.value = null
    error.value = null
    audio.value = null
    audioError.value = null
    audioLoading.value = false
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
    llmRunning,
    summaryRunning,
    load,
    exportAs,
    reset,
    applyOps,
    editText,
    flush,
    saveSpeakers,
    nextSpeakerId,
    revert,
    suggestWithLlm,
    summarize,
    removeSummary,
  }
})
