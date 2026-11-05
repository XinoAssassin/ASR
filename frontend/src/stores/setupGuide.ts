/**
 * Optional first-run setup guide (web and desktop): a checklist whose steps read real state —
 * (a) backend connected, (b) recognition ready (local models or a cloud key), (c) try the bundled
 * sample meeting, (d) optional term set, (e) desktop only: recording sources + auto-stop.
 *
 * Opening: once at startup when not dismissed, not skipped in this session, and the active
 * backend has no jobs yet (someone who already transcribed does not need it; an unreachable
 * backend does). The bundled edition's first-run wizard goes first. The top bar help button and
 * Settings reopen it any time.
 *
 * "跳过" closes it for this session. "不再显示" is stored globally (this browser / app), not per
 * backend profile: it is about the person, who does not want the onboarding again when adding a
 * remote backend; the step states are per backend anyway. The sample job is remembered per
 * profile (`asr.setupGuide.sample@<profileId>`), so the step stays done and a running job is
 * picked up again after a reload.
 */
import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'

import {
  activeProfile,
  createJob,
  getJob,
  isTerminal,
  listJobs,
  profileStorageKey,
  readStorage,
  toApiError,
  uploadFileChunked,
  watchJob,
  writeStorage,
  type ApiError,
  type JobOut,
  type JobStream,
} from '@/api'
import { isDesktop } from '@/platform'
import { buildJobCreate, defaultJobForm } from '@/utils/jobForm'
import { parseStoredSample, recognitionState, type SamplePhase, type StoredSample } from '@/utils/setupGuide'

import { useHealthStore } from './health'
import { useManagedBackendStore } from './managedBackend'
import { useProfilesStore } from './profiles'
import { useRecorderStore } from './recorder'
import { useSettingsStore } from './settings'
import { useTermSetsStore } from './termSets'
import { useUploadsStore } from './uploads'

export const DISMISSED_KEY = 'asr.setupGuide.dismissed'
export const SAMPLE_KEY = 'asr.setupGuide.sample'
/** Bundled with the SPA (`public/samples/`, synthetic speech, see its README). */
export const SAMPLE_FILE = 'meeting-sample.ogg'
/** Upload target when the user has not picked one yet (relative = under the first upload root). */
export const SAMPLE_TARGET_DIR = 'samples'

export type StepId = 'backend' | 'recognition' | 'sample' | 'terms' | 'recorder'
export type StepState = 'done' | 'todo' | 'pending'

function sampleUrl(): string {
  const base = import.meta.env.BASE_URL || '/'
  return `${base.endsWith('/') ? base : `${base}/`}samples/${SAMPLE_FILE}`
}

function sampleKey(): string {
  return profileStorageKey(SAMPLE_KEY, activeProfile().id)
}

export const useSetupGuideStore = defineStore('setupGuide', () => {
  const desktop = isDesktop()
  const health = useHealthStore()
  const settings = useSettingsStore()
  const termSets = useTermSetsStore()

  const open = ref(false)
  const dismissed = ref(readStorage(DISMISSED_KEY) === '1')
  /** "跳过" this session. */
  const skipped = ref(false)
  const modelsLoaded = ref(false)

  // -- sample job ------------------------------------------------------------------------------
  const stored = ref<StoredSample | null>(parseStoredSample(readStorage(sampleKey())))
  const phase = ref<SamplePhase>(stored.value?.transcriptId ? 'succeeded' : 'idle')
  /** Upload progress 0..1. */
  const uploadProgress = ref(0)
  const job = ref<JobOut | null>(null)
  const sampleError = ref<ApiError | null>(null)
  let stream: JobStream | null = null
  let abort: AbortController | null = null
  /** Bumped on a backend switch: work for the previous backend stops touching state. */
  let generation = 0

  const transcriptId = computed(() => stored.value?.transcriptId ?? job.value?.transcript_id ?? null)
  const sampleBusy = computed(() => ['fetching', 'uploading', 'starting', 'running'].includes(phase.value))

  function remember(value: StoredSample | null): void {
    stored.value = value
    writeStorage(sampleKey(), value ? JSON.stringify(value) : null)
  }

  // -- step states -----------------------------------------------------------------------------
  const recognition = computed(() =>
    recognitionState(settings.server, settings.localModels, modelsLoaded.value),
  )

  const steps = computed<Record<StepId, StepState>>(() => {
    const backend: StepState =
      health.state === 'connected' && !health.unauthorized
        ? 'done'
        : health.state === 'checking' || health.state === 'idle'
          ? 'pending'
          : 'todo'
    const rec = recognition.value
    const recognitionStep: StepState =
      rec.cloudReady || rec.localReady ? 'done' : backend === 'done' && !rec.loaded ? 'pending' : 'todo'
    return {
      backend,
      recognition: recognitionStep,
      sample: transcriptId.value && phase.value === 'succeeded' ? 'done' : sampleBusy.value ? 'pending' : 'todo',
      terms: termSets.items.length ? 'done' : 'todo',
      recorder: desktop && useRecorderStore().recordings.length ? 'done' : 'todo',
    }
  })

  const visibleSteps = computed<StepId[]>(() =>
    desktop ? ['backend', 'recognition', 'sample', 'terms', 'recorder'] : ['backend', 'recognition', 'sample', 'terms'],
  )
  /** The required steps (a–c) are done. */
  const complete = computed(() => (['backend', 'recognition', 'sample'] as const).every((s) => steps.value[s] === 'done'))
  const doneCount = computed(() => visibleSteps.value.filter((s) => steps.value[s] === 'done').length)
  const canRunSample = computed(
    () => steps.value.backend === 'done' && !!recognition.value.sampleProvider && !sampleBusy.value,
  )

  // -- data ------------------------------------------------------------------------------------
  /** Read everything the checklist shows from the active backend. */
  async function refresh(): Promise<void> {
    await health.check()
    if (health.state !== 'connected') return
    await Promise.allSettled([
      settings.load(),
      settings.loadLocalModels().then(() => (modelsLoaded.value = true)),
      termSets.load(),
      resumeSample(),
    ])
  }

  /** A remembered sample job: re-read it (finished meanwhile, still running, or gone). */
  async function resumeSample(): Promise<void> {
    const s = stored.value
    if (!s || stream || sampleBusy.value) return
    const gen = generation
    try {
      const j = await getJob(s.jobId)
      if (gen !== generation) return
      applyJob(j)
      if (!isTerminal(j.status)) follow(j.id)
    } catch (e) {
      if (gen !== generation) return
      // Deleted on the server: the step is open again.
      if (toApiError(e).status === 404) {
        remember(null)
        phase.value = 'idle'
        job.value = null
      }
    }
  }

  function applyJob(j: JobOut): void {
    job.value = j
    if (j.status === 'succeeded') {
      phase.value = 'succeeded'
      remember({ jobId: j.id, transcriptId: j.transcript_id ?? null })
    } else if (j.status === 'failed' || j.status === 'cancelled') {
      phase.value = 'failed'
      sampleError.value = null
    } else {
      phase.value = 'running'
    }
  }

  function follow(jobId: string): void {
    stream?.stop()
    const gen = generation
    stream = watchJob(jobId, {
      onJob: (j) => {
        if (gen === generation) applyJob(j)
      },
      onError: (e) => {
        if (gen !== generation) return
        sampleError.value = e
        phase.value = 'failed'
      },
    })
    void stream.done.catch(() => undefined).finally(() => {
      if (gen === generation) stream = null
    })
  }

  /** Upload the bundled sample through the normal chunked upload, then run a job on it. */
  async function runSample(): Promise<void> {
    const provider = recognition.value.sampleProvider
    if (!provider || sampleBusy.value) return
    const gen = generation
    const live = (): boolean => gen === generation
    abort = new AbortController()
    sampleError.value = null
    job.value = null
    uploadProgress.value = 0
    phase.value = 'fetching'
    try {
      const response = await fetch(sampleUrl(), { signal: abort.signal })
      if (!response.ok) throw new Error(`${response.status} ${response.statusText}`.trim())
      const blob = await response.blob()
      const file = new File([blob], SAMPLE_FILE, { type: blob.type || 'audio/ogg' })
      if (!live()) return
      phase.value = 'uploading'
      const dir = useUploadsStore().targetDir.trim() || SAMPLE_TARGET_DIR
      const upload = await uploadFileChunked(file, dir, {
        signal: abort.signal,
        onProgress: (p) => {
          if (live()) uploadProgress.value = p.total ? p.loaded / p.total : 0
        },
      })
      if (!live()) return
      phase.value = 'starting'
      const body = buildJobCreate(
        defaultJobForm(provider),
        { uploadId: upload.id, channels: 'auto', streamIndex: upload.probe?.stream_index ?? 0 },
        [],
      )
      const created = await createJob(body)
      if (!live()) return
      remember({ jobId: created.id, transcriptId: null })
      applyJob(created)
      if (!isTerminal(created.status)) follow(created.id)
    } catch (e) {
      if (!live()) return
      sampleError.value = toApiError(e)
      phase.value = 'failed'
    } finally {
      abort = null
    }
  }

  // -- opening ---------------------------------------------------------------------------------
  function openGuide(): void {
    open.value = true
    void refresh()
  }

  /** "跳过": close for this session. */
  function skip(): void {
    skipped.value = true
    open.value = false
  }

  /** "不再显示": never open by itself again (global; the help button still opens it). */
  function dismiss(): void {
    dismissed.value = true
    writeStorage(DISMISSED_KEY, '1')
    open.value = false
  }

  /** Close without changing anything (e.g. to follow a link). */
  function close(): void {
    open.value = false
  }

  /** Automatic opening at startup (after the bundled edition's wizard had its chance). */
  async function startup(): Promise<void> {
    if (dismissed.value || skipped.value || open.value) return
    if (desktop && useManagedBackendStore().wizardOpen) return
    if (stored.value?.transcriptId) return
    try {
      const jobs = await listJobs({ page: 1, page_size: 1 })
      if (jobs.total > 0) return
    } catch {
      // Unreachable / unauthorised backend: exactly what the guide helps with.
    }
    if (dismissed.value || skipped.value) return
    openGuide()
  }

  /** Another backend (profile switch): its own sample state; running work is dropped. */
  function resetForBackend(): void {
    generation++
    stream?.stop()
    stream = null
    abort?.abort()
    abort = null
    stored.value = parseStoredSample(readStorage(sampleKey()))
    phase.value = stored.value?.transcriptId ? 'succeeded' : 'idle'
    job.value = null
    sampleError.value = null
    uploadProgress.value = 0
    modelsLoaded.value = false
    if (open.value) void refresh()
  }

  watch(
    () => useProfilesStore().epoch,
    () => resetForBackend(),
  )

  // Desktop + backend edition: once its first-run wizard closes (done or "稍后"), the guide
  // takes over (models, sample) unless it was dismissed / skipped.
  if (desktop) {
    watch(
      () => useManagedBackendStore().wizardOpen,
      (now, before) => {
        if (before && !now) void startup()
      },
    )
  }

  return {
    open,
    dismissed,
    skipped,
    steps,
    visibleSteps,
    complete,
    doneCount,
    recognition,
    phase,
    uploadProgress,
    job,
    sampleError,
    transcriptId,
    sampleBusy,
    canRunSample,
    refresh,
    runSample,
    openGuide,
    skip,
    dismiss,
    close,
    startup,
    resetForBackend,
  }
})
