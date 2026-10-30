/**
 * Jobs list with server-side pagination. Live updates: one SSE stream per non-terminal job on the
 * current page (capped, because browsers allow ~6 HTTP/1.1 connections per origin); anything over
 * the cap is refreshed by polling the list. Everything is re-read from the API on load, so state
 * survives page reloads.
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import {
  cancelJob,
  deleteJob,
  getUpload,
  isTerminal,
  listJobs,
  retryJob,
  toApiError,
  watchJob,
  type ApiError,
  type JobOut,
  type JobStatus,
  type JobStream,
  type UploadOut,
} from '@/api'

const MAX_STREAMS = 4
const POLL_MS = 5000

export const useJobsStore = defineStore('jobs', () => {
  const items = ref<JobOut[]>([])
  const total = ref(0)
  const page = ref(1)
  const pageSize = ref(20)
  const status = ref<JobStatus | null>(null)
  const loading = ref(false)
  const error = ref<ApiError | null>(null)
  /** upload id → UploadOut (null = lookup failed); used for file name and duration columns. */
  const uploads = ref<Record<string, UploadOut | null>>({})

  const streams = new Map<string, JobStream>()
  /**
   * Jobs whose stream failed for good (401, 404, a proxy answering HTML): refreshed by polling
   * the list instead of reopening a stream that would fail the same way.
   */
  const failed = new Set<string>()
  let pollTimer: ReturnType<typeof setInterval> | null = null
  let active = false
  /** Incremented per list request; only the latest one may write the list. */
  let loadSeq = 0

  const hasActiveJobs = computed(() => items.value.some((j) => !isTerminal(j.status)))

  function upsert(job: JobOut): void {
    const index = items.value.findIndex((j) => j.id === job.id)
    if (index >= 0) items.value.splice(index, 1, job)
  }

  async function fetchUploads(jobs: JobOut[]): Promise<void> {
    const missing = [
      ...new Set(jobs.map((j) => j.upload_id).filter((id): id is string => !!id)),
    ].filter((id) => !(id in uploads.value))
    await Promise.all(
      missing.map(async (id) => {
        try {
          uploads.value[id] = await getUpload(id)
        } catch {
          uploads.value[id] = null
        }
      }),
    )
  }

  async function load(): Promise<void> {
    const seq = ++loadSeq
    loading.value = true
    try {
      const result = await listJobs({
        page: page.value,
        page_size: pageSize.value,
        status: status.value,
      })
      // An older request (previous page / filter, or a poll) answering late is ignored.
      if (seq !== loadSeq) return
      items.value = result.items
      total.value = result.total
      error.value = null
      void fetchUploads(result.items)
    } catch (e) {
      if (seq === loadSeq) error.value = toApiError(e)
    } finally {
      if (seq === loadSeq) {
        loading.value = false
        if (active) syncLive()
      }
    }
  }

  function stopStream(jobId: string): void {
    streams.get(jobId)?.stop()
    streams.delete(jobId)
  }

  function streamFailed(jobId: string): void {
    stopStream(jobId)
    failed.add(jobId)
    if (active) syncLive()
  }

  /** Open / close streams so that they match the non-terminal jobs on this page. */
  function syncLive(): void {
    const live = items.value.filter((j) => !isTerminal(j.status)).map((j) => j.id)
    for (const id of [...streams.keys()]) if (!live.includes(id)) stopStream(id)
    for (const id of [...failed]) if (!live.includes(id)) failed.delete(id)
    for (const id of live) {
      if (streams.size >= MAX_STREAMS) break
      if (streams.has(id) || failed.has(id)) continue
      const stream = watchJob(id, {
        onJob: (job) => {
          upsert(job)
          if (isTerminal(job.status)) stopStream(job.id)
        },
        onError: () => streamFailed(id),
      })
      streams.set(id, stream)
      stream.done.catch(() => streamFailed(id))
    }
    // Over the stream cap, or a stream that failed: those jobs are kept current by polling.
    const overflow = live.some((id) => !streams.has(id))
    if (overflow && !pollTimer) pollTimer = setInterval(() => void load(), POLL_MS)
    if (!overflow && pollTimer) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  }

  /** Call from the Jobs view on mount. */
  function startLive(): void {
    active = true
    syncLive()
  }

  /** Call from the Jobs view on unmount. */
  function stopLive(): void {
    active = false
    for (const id of [...streams.keys()]) stopStream(id)
    failed.clear() // a new visit tries the streams again
    if (pollTimer) clearInterval(pollTimer)
    pollTimer = null
  }

  async function setPage(p: number): Promise<void> {
    page.value = p
    await load()
  }

  async function setPageSize(size: number): Promise<void> {
    pageSize.value = size
    page.value = 1
    await load()
  }

  async function setStatus(s: JobStatus | null): Promise<void> {
    status.value = s
    page.value = 1
    await load()
  }

  async function cancel(jobId: string): Promise<void> {
    upsert(await cancelJob(jobId))
    if (active) syncLive()
  }

  async function retry(jobId: string): Promise<void> {
    upsert(await retryJob(jobId))
    if (active) syncLive()
  }

  async function remove(jobId: string, deleteAudio: boolean): Promise<void> {
    await deleteJob(jobId, deleteAudio)
    stopStream(jobId)
    await load()
  }

  return {
    items,
    total,
    page,
    pageSize,
    status,
    loading,
    error,
    uploads,
    hasActiveJobs,
    load,
    startLive,
    stopLive,
    setPage,
    setPageSize,
    setStatus,
    cancel,
    retry,
    remove,
  }
})
