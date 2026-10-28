/**
 * Transcript page: the transcript, its job (for the upload id) and the audio Blob for the
 * waveform. Audio is fetched with the X-API-Key header (D6), never via a URL with the key.
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import {
  audioPath,
  downloadExport,
  fetchBlob,
  getJob,
  getTranscript,
  isAbortError,
  toApiError,
  type ApiError,
  type ExportOptions,
  type JobOut,
  type Speaker,
  type TranscriptOut,
} from '@/api'

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

  async function exportAs(options: ExportOptions): Promise<string> {
    if (!transcript.value) throw new Error('No transcript')
    exporting.value = true
    try {
      return await downloadExport(transcript.value.id, options)
    } finally {
      exporting.value = false
    }
  }

  function reset(): void {
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
    load,
    exportAs,
    reset,
  }
})
