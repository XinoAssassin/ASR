import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick, reactive } from 'vue'

const h = vi.hoisted(() => ({
  storage: new Map<string, string>(),
  profileId: 'local',
  api: {
    listJobs: vi.fn(),
    getJob: vi.fn(),
    createJob: vi.fn(),
    uploadFileChunked: vi.fn(),
    watchJob: vi.fn(),
  },
}))

vi.mock('@/api', async () => {
  const errors = await import('@/api/errors')
  return {
    ...h.api,
    readStorage: (k: string) => h.storage.get(k) ?? null,
    writeStorage: (k: string, v: string | null) => (v ? h.storage.set(k, v) : h.storage.delete(k)),
    activeProfile: () => ({ id: h.profileId }),
    profileStorageKey: (k: string, id: string) => `${k}@${id}`,
    isTerminal: (s: string) => ['succeeded', 'failed', 'cancelled'].includes(s),
    toApiError: errors.toApiError,
  }
})

const fakes = vi.hoisted(() => ({
  health: null as unknown as { state: string; unauthorized: boolean; check: () => Promise<void> },
  settings: null as unknown as Record<string, unknown>,
  termSets: null as unknown as { items: unknown[]; load: () => Promise<void> },
  profiles: null as unknown as { epoch: number },
  managed: null as unknown as { wizardOpen: boolean },
}))

vi.mock('./health', () => ({ useHealthStore: () => fakes.health }))
vi.mock('./settings', () => ({ useSettingsStore: () => fakes.settings }))
vi.mock('./termSets', () => ({ useTermSetsStore: () => fakes.termSets }))
vi.mock('./profiles', () => ({ useProfilesStore: () => fakes.profiles }))
vi.mock('./managedBackend', () => ({ useManagedBackendStore: () => fakes.managed }))
vi.mock('./uploads', () => ({ useUploadsStore: () => ({ targetDir: '' }) }))
vi.mock('./recorder', () => ({ useRecorderStore: () => ({ recordings: [] }) }))

const { DISMISSED_KEY, useSetupGuideStore } = await import('./setupGuide')

const READY_MODELS = [
  { id: 'fsmn-vad', kind: 'vad', downloaded: true },
  { id: 'qwen3-asr-0.6b', kind: 'asr', downloaded: true },
  { id: 'qwen3-forced-aligner', kind: 'aligner', downloaded: true },
  { id: 'campplus', kind: 'diarization', downloaded: true },
]

function job(patch: Record<string, unknown> = {}) {
  return { id: 'job-1', status: 'queued', stage: 'queued', progress: 0, transcript_id: null, error: null, ...patch }
}

let onJob: ((j: unknown) => void) | null = null

beforeEach(() => {
  h.storage.clear()
  h.profileId = 'local'
  for (const fn of Object.values(h.api)) fn.mockReset()
  fakes.health = reactive({ state: 'connected', unauthorized: false, check: vi.fn(async () => undefined) })
  fakes.settings = reactive({
    server: { provider_default: 'local', cloud: { api_key_set: false }, local: { asr_model: null, diarize_model: null } },
    localModels: READY_MODELS,
    load: vi.fn(async () => undefined),
    loadLocalModels: vi.fn(async () => undefined),
  })
  fakes.termSets = reactive({ items: [], load: vi.fn(async () => undefined) })
  fakes.profiles = reactive({ epoch: 0 })
  fakes.managed = reactive({ wizardOpen: false })
  h.api.watchJob.mockImplementation((_id: string, o: { onJob: (j: unknown) => void }) => {
    onJob = o.onJob
    return { stop: vi.fn(), done: new Promise(() => undefined) }
  })
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(new Blob(['OggS'], { type: 'audio/ogg' }), { status: 200 })),
  )
  setActivePinia(createPinia())
})

afterEach(() => vi.unstubAllGlobals())

describe('setup guide store', () => {
  it('opens at startup on a backend without jobs, not when it has some', async () => {
    h.api.listJobs.mockResolvedValue({ total: 0, items: [] })
    const guide = useSetupGuideStore()
    await guide.startup()
    expect(guide.open).toBe(true)

    setActivePinia(createPinia())
    h.api.listJobs.mockResolvedValue({ total: 3, items: [] })
    const again = useSetupGuideStore()
    await again.startup()
    expect(again.open).toBe(false)
  })

  it('opens when the backend is unreachable (that is what it helps with)', async () => {
    h.api.listJobs.mockRejectedValue(new TypeError('Failed to fetch'))
    const guide = useSetupGuideStore()
    await guide.startup()
    expect(guide.open).toBe(true)
  })

  it('"跳过" lasts for the session, "不再显示" is stored', async () => {
    h.api.listJobs.mockResolvedValue({ total: 0, items: [] })
    const guide = useSetupGuideStore()
    guide.openGuide()
    guide.skip()
    await guide.startup()
    expect(guide.open).toBe(false)
    guide.openGuide() // help button still works
    expect(guide.open).toBe(true)
    guide.dismiss()
    expect(h.storage.get(DISMISSED_KEY)).toBe('1')
    setActivePinia(createPinia())
    const next = useSetupGuideStore()
    await next.startup()
    expect(next.open).toBe(false)
    expect(h.api.listJobs).not.toHaveBeenCalled() // skipped / dismissed: not even asked
  })

  it('leaves the startup to the managed-backend wizard, then takes over', async () => {
    vi.stubGlobal('window', { __TAURI_INTERNALS__: {} })
    fakes.managed.wizardOpen = true
    h.api.listJobs.mockResolvedValue({ total: 0, items: [] })
    const guide = useSetupGuideStore()
    await guide.startup()
    expect(guide.open).toBe(false)
    expect(guide.visibleSteps).toContain('recorder')
    // Wizard closed (installed or "稍后"): the guide opens for models + sample.
    fakes.managed.wizardOpen = false
    await nextTick()
    await vi.waitFor(() => expect(guide.open).toBe(true))
  })

  it('derives the step states from the live stores', async () => {
    const guide = useSetupGuideStore()
    await guide.refresh()
    expect(guide.steps).toMatchObject({ backend: 'done', recognition: 'done', sample: 'todo', terms: 'todo' })
    expect(guide.recognition.sampleProvider).toBe('local')
    fakes.health.state = 'error'
    expect(guide.steps.backend).toBe('todo')
    fakes.health.state = 'connected'
    fakes.health.unauthorized = true
    expect(guide.steps.backend).toBe('todo')
    fakes.termSets.items = [{ id: 't' }]
    expect(guide.steps.terms).toBe('done')
    expect(guide.visibleSteps).not.toContain('recorder') // web build
  })

  it('uploads the bundled sample, runs a job with the ready provider and remembers it', async () => {
    h.api.uploadFileChunked.mockImplementation(async (file: File, dir: string, o: { onProgress: (p: unknown) => void }) => {
      o.onProgress({ loaded: 2, total: 4 })
      expect(file.name).toBe('meeting-sample.ogg')
      expect(dir).toBe('samples')
      return { id: 'up-1', probe: { stream_index: 0 } }
    })
    h.api.createJob.mockResolvedValue(job())
    const guide = useSetupGuideStore()
    await guide.refresh()
    expect(guide.canRunSample).toBe(true)
    await guide.runSample()
    expect(vi.mocked(fetch).mock.calls[0]![0]).toBe('/samples/meeting-sample.ogg')
    const body = h.api.createJob.mock.calls[0]![0]
    expect(body).toMatchObject({ upload_id: 'up-1', provider: 'local' })
    expect(guide.phase).toBe('running')
    expect(guide.steps.sample).toBe('pending')
    onJob!(job({ status: 'running', stage: 'asr', progress: 0.5 }))
    expect(guide.job?.stage).toBe('asr')
    onJob!(job({ status: 'succeeded', stage: 'done', progress: 1, transcript_id: 'tr-1' }))
    expect(guide.phase).toBe('succeeded')
    expect(guide.transcriptId).toBe('tr-1')
    expect(guide.steps.sample).toBe('done')
    expect(JSON.parse(h.storage.get('asr.setupGuide.sample@local')!)).toEqual({ jobId: 'job-1', transcriptId: 'tr-1' })
    // Done on this backend: no automatic opening any more.
    setActivePinia(createPinia())
    const later = useSetupGuideStore()
    await later.startup()
    expect(later.open).toBe(false)
    expect(later.steps.sample).toBe('done')
  })

  it('reports a failed upload and allows a retry', async () => {
    h.api.uploadFileChunked.mockRejectedValue(new Error('disk full'))
    const guide = useSetupGuideStore()
    await guide.refresh()
    await guide.runSample()
    expect(guide.phase).toBe('failed')
    expect(guide.sampleError?.message).toContain('disk full')
    expect(guide.canRunSample).toBe(true)
  })

  it('cannot run the sample without a ready provider', async () => {
    fakes.settings.localModels = []
    const guide = useSetupGuideStore()
    await guide.refresh()
    expect(guide.steps.recognition).toBe('todo')
    expect(guide.canRunSample).toBe(false)
    await guide.runSample()
    expect(h.api.uploadFileChunked).not.toHaveBeenCalled()
  })

  it('picks a remembered running job up again and forgets a deleted one', async () => {
    h.storage.set('asr.setupGuide.sample@local', JSON.stringify({ jobId: 'job-1', transcriptId: null }))
    h.api.getJob.mockResolvedValue(job({ status: 'running', stage: 'diarizing' }))
    const guide = useSetupGuideStore()
    await guide.refresh()
    expect(guide.phase).toBe('running')
    expect(h.api.watchJob).toHaveBeenCalledWith('job-1', expect.anything())

    setActivePinia(createPinia())
    const { ApiError } = await import('@/api/errors')
    h.api.getJob.mockRejectedValue(new ApiError(404, 'job not found'))
    const other = useSetupGuideStore()
    await other.refresh()
    expect(other.phase).toBe('idle')
    expect(h.storage.has('asr.setupGuide.sample@local')).toBe(false)
  })

  it('switches the sample state with the backend profile', async () => {
    h.storage.set('asr.setupGuide.sample@remote', JSON.stringify({ jobId: 'j9', transcriptId: 't9' }))
    const guide = useSetupGuideStore()
    expect(guide.steps.sample).toBe('todo')
    h.profileId = 'remote'
    fakes.profiles.epoch++
    await nextTick()
    expect(guide.transcriptId).toBe('t9')
    expect(guide.steps.sample).toBe('done')
  })
})
