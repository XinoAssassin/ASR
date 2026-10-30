/**
 * Upload page state: target directory (+ recent list), the local file queue with per-file chunk
 * progress, and the uploads that are on the server and waiting for a job (with the per-file
 * channel override chosen on the probe card).
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import {
  createJob,
  isAbortError,
  readStorage,
  registerUpload,
  toApiError,
  uploadFileChunked,
  writeStorage,
  type ApiError,
  type ChannelsOption,
  type Term,
  type UploadOut,
  type UploadSession,
} from '@/api'
import { isAcceptedFile } from '@/utils/audio'
import { buildJobCreate, defaultJobForm, type JobFormState } from '@/utils/jobForm'

const RECENT_DIRS_KEY = 'asr.recentDirs'
const LAST_DIR_KEY = 'asr.lastTargetDir'
const MAX_RECENT = 8

export type QueueStatus = 'pending' | 'uploading' | 'done' | 'error' | 'cancelled'

export interface QueueItem {
  key: string
  file: File
  status: QueueStatus
  loaded: number
  total: number
  part: number
  totalParts: number
  error: ApiError | null
  /** Server session of the last attempt: a retry after an error / cancel resumes it. */
  session: UploadSession | null
}

/** Statuses that "Upload" (re)starts; `pendingCount` counts exactly these. */
const STARTABLE: readonly QueueStatus[] = ['pending', 'error', 'cancelled']

export interface JobStartFailure {
  name: string
  error: ApiError
}

export interface ReadyUpload {
  upload: UploadOut
  source: 'uploaded' | 'registered'
  channels: ChannelsOption
  streamIndex: number
}

function readRecent(): string[] {
  try {
    const parsed: unknown = JSON.parse(readStorage(RECENT_DIRS_KEY) ?? '[]')
    return Array.isArray(parsed) ? parsed.filter((d): d is string => typeof d === 'string') : []
  } catch {
    return []
  }
}

let keySeq = 0

export const useUploadsStore = defineStore('uploads', () => {
  const targetDir = ref(readStorage(LAST_DIR_KEY) ?? '')
  const recentDirs = ref<string[]>(readRecent())
  const queue = ref<QueueItem[]>([])
  const ready = ref<ReadyUpload[]>([])
  const uploading = ref(false)
  const registering = ref(false)
  const controllers = new Map<string, AbortController>()
  /** Job options form; kept here so it survives navigating away and back. */
  const jobForm = ref<JobFormState>(defaultJobForm())
  /** Whether jobForm.provider has been initialised from the server default. */
  const providerInitialized = ref(false)

  const pendingCount = computed(
    () => queue.value.filter((i) => STARTABLE.includes(i.status)).length,
  )

  function rememberDir(dir: string): void {
    const clean = dir.trim()
    if (!clean) return
    writeStorage(LAST_DIR_KEY, clean)
    recentDirs.value = [clean, ...recentDirs.value.filter((d) => d !== clean)].slice(0, MAX_RECENT)
    writeStorage(RECENT_DIRS_KEY, JSON.stringify(recentDirs.value))
  }

  /** Queue files; returns the names that were rejected by extension. */
  function addFiles(files: Iterable<File>): string[] {
    const rejected: string[] = []
    for (const file of files) {
      if (!isAcceptedFile(file.name)) {
        rejected.push(file.name)
        continue
      }
      const same = (i: QueueItem): boolean =>
        i.file.name === file.name &&
        i.file.size === file.size &&
        i.file.lastModified === file.lastModified
      // Queued or uploading already: skip. Done items may be uploaded again on purpose.
      if (queue.value.some((i) => same(i) && (i.status === 'pending' || i.status === 'uploading'))) {
        continue
      }
      const fresh: QueueItem = {
        key: `f${++keySeq}`,
        file,
        status: 'pending',
        loaded: 0,
        total: file.size,
        part: 0,
        totalParts: 0,
        error: null,
        session: null,
      }
      // A failed / cancelled copy is replaced (in place) by the newly picked file.
      const stale = queue.value.findIndex(
        (i) => same(i) && (i.status === 'error' || i.status === 'cancelled'),
      )
      if (stale >= 0) queue.value.splice(stale, 1, fresh)
      else queue.value.push(fresh)
    }
    return rejected
  }

  function item(key: string): QueueItem | undefined {
    return queue.value.find((i) => i.key === key)
  }

  function removeItem(key: string): void {
    controllers.get(key)?.abort()
    queue.value = queue.value.filter((i) => i.key !== key)
  }

  function cancelItem(key: string): void {
    controllers.get(key)?.abort()
  }

  function addReady(upload: UploadOut, source: ReadyUpload['source']): void {
    ready.value = [
      ...ready.value.filter((r) => r.upload.id !== upload.id),
      { upload, source, channels: 'auto', streamIndex: upload.probe?.stream_index ?? 0 },
    ]
  }

  async function uploadOne(entry: QueueItem, dir: string): Promise<void> {
    const controller = new AbortController()
    controllers.set(entry.key, controller)
    const live = (): QueueItem | undefined => item(entry.key)
    Object.assign(entry, { status: 'uploading', error: null, loaded: 0 })
    try {
      const upload = await uploadFileChunked(entry.file, dir, {
        signal: controller.signal,
        resume: entry.session,
        onSession: (session) => {
          const it = live()
          if (it) it.session = session
        },
        onProgress: (p) => {
          const it = live()
          if (it) Object.assign(it, p)
        },
      })
      const it = live()
      if (it) Object.assign(it, { status: 'done', loaded: it.total, session: null })
      addReady(upload, 'uploaded')
    } catch (e) {
      const it = live()
      if (it) {
        it.status = isAbortError(e) ? 'cancelled' : 'error'
        it.error = isAbortError(e) ? null : toApiError(e)
      }
    } finally {
      controllers.delete(entry.key)
    }
  }

  /** Upload every pending / failed / cancelled item (what `pendingCount` shows) into `targetDir`. */
  async function uploadAll(): Promise<void> {
    const dir = targetDir.value.trim()
    if (!dir || uploading.value) return
    uploading.value = true
    rememberDir(dir)
    try {
      for (const entry of queue.value) {
        if (STARTABLE.includes(entry.status)) await uploadOne(entry, dir)
      }
    } finally {
      uploading.value = false
    }
  }

  /** "File already on the server": throws ApiError for the caller to display. */
  async function register(path: string): Promise<UploadOut> {
    registering.value = true
    try {
      const upload = await registerUpload(path.trim())
      addReady(upload, 'registered')
      return upload
    } finally {
      registering.value = false
    }
  }

  function removeReady(uploadId: string): void {
    ready.value = ready.value.filter((r) => r.upload.id !== uploadId)
  }

  /** After jobs were created: forget the submitted uploads and finished queue items. */
  function clearSubmitted(uploadIds: string[]): void {
    ready.value = ready.value.filter((r) => !uploadIds.includes(r.upload.id))
    queue.value = queue.value.filter((i) => i.status !== 'done')
  }

  const submitting = ref(false)

  /**
   * Create one job per ready upload with the shared form (+ `terms` parsed from the extra-terms
   * box). Submitted uploads leave the ready list; failures stay there and are returned.
   */
  async function startJobs(terms: Term[]): Promise<{ submitted: number; failures: JobStartFailure[] }> {
    if (submitting.value) return { submitted: 0, failures: [] }
    submitting.value = true
    const submitted: string[] = []
    const failures: JobStartFailure[] = []
    try {
      for (const entry of [...ready.value]) {
        const body = buildJobCreate(
          jobForm.value,
          { uploadId: entry.upload.id, channels: entry.channels, streamIndex: entry.streamIndex },
          terms,
        )
        try {
          await createJob(body)
          submitted.push(entry.upload.id)
        } catch (e) {
          failures.push({ name: entry.upload.original_name, error: toApiError(e) })
        }
      }
    } finally {
      submitting.value = false
    }
    if (submitted.length) clearSubmitted(submitted)
    return { submitted: submitted.length, failures }
  }

  return {
    targetDir,
    recentDirs,
    queue,
    ready,
    uploading,
    registering,
    jobForm,
    providerInitialized,
    pendingCount,
    rememberDir,
    addFiles,
    removeItem,
    cancelItem,
    uploadAll,
    register,
    removeReady,
    clearSubmitted,
    submitting,
    startJobs,
  }
})
