/**
 * Backend profiles: migration of the single URL / key, per-profile keys on every transport,
 * switching (flush + confirm, per-backend resets), desktop push via the fake platform. Runs the
 * real `@/api` against a stubbed window + fetch.
 */
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setDesktopPlatformForTests } from '@/platform'
import { createFakeDesktop, type FakeDesktop } from '@/platform/testing'

const storage = new Map<string, string>()

interface Call {
  method: string
  url: string
  key: string | null
}
const calls: Call[] = []
/** Answer for a request; default 200 `{}`. */
let respond: (method: string, url: string) => Response | Promise<Response> = () => json({})

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

async function fetchMock(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const request = input instanceof Request ? input : new Request(String(input), init)
  calls.push({ method: request.method, url: request.url, key: request.headers.get('X-API-Key') })
  return respond(request.method, request.url)
}

function stubWindow(desktop = false): void {
  vi.stubGlobal('window', {
    ...(desktop ? { __TAURI_INTERNALS__: {} } : {}),
    location: { href: 'http://spa.local:5173/jobs' },
    localStorage: {
      getItem: (k: string) => storage.get(k) ?? null,
      setItem: (k: string, v: string) => storage.set(k, v),
      removeItem: (k: string) => storage.delete(k),
    },
  })
  vi.stubGlobal('fetch', fetchMock)
}

stubWindow()
const client = await import('@/api/client')
const { LOCAL_PROFILE_ID, STORAGE_KEYS, getApiKey, getBackendUrl, authHeaders, readProfiles } = client
const { useProfilesStore } = await import('./profiles')
const { useJobsStore } = await import('./jobs')
const { useTermSetsStore } = await import('./termSets')
const { useUploadsStore } = await import('./uploads')
const { useTranscriptStore } = await import('./transcript')
const { useHealthStore } = await import('./health')
const { useHardwareStore } = await import('./hardware')
const { useRecorderStore } = await import('./recorder')

beforeEach(() => {
  storage.clear()
  calls.length = 0
  respond = (_m, url) => (url.includes('/api/health') ? json({ status: 'ok', version: 't', decoder: 'pyav', ffmpeg_version: null, api_key_required: true }) : json({}))
  stubWindow()
  setActivePinia(createPinia())
})

afterEach(() => {
  setDesktopPlatformForTests(null)
  vi.unstubAllGlobals()
})

const flushPromises = () => new Promise((r) => setTimeout(r, 0))

describe('migration of the single backend URL / key', () => {
  it('a remote URL becomes an active remote profile with the key; per-backend dirs move with it', () => {
    storage.set(STORAGE_KEYS.backendUrl, 'nas.lan:8000')
    storage.set(STORAGE_KEYS.apiKey, ' secret ')
    storage.set('asr.lastTargetDir', '/volume1/rec')
    const state = readProfiles()
    expect(state.profiles.map((p) => p.id)[0]).toBe(LOCAL_PROFILE_ID)
    const remote = state.profiles[1]!
    expect(remote).toMatchObject({ name: 'nas.lan:8000', url: 'http://nas.lan:8000', apiKey: 'secret' })
    expect(state.activeId).toBe(remote.id)
    expect(state.profiles[0]!.apiKey).toBeNull()
    expect(getBackendUrl()).toBe('http://nas.lan:8000')
    expect(getApiKey()).toBe('secret')
    // Legacy keys are gone; the directory is now the remote profile's.
    expect(storage.has(STORAGE_KEYS.backendUrl)).toBe(false)
    expect(storage.has(STORAGE_KEYS.apiKey)).toBe(false)
    expect(storage.get(`asr.lastTargetDir@${remote.id}`)).toBe('/volume1/rec')
    expect(storage.has('asr.lastTargetDir')).toBe(false)
  })

  it('no URL or a loopback one goes to the built-in profile (the default URL is not pinned)', () => {
    storage.set(STORAGE_KEYS.apiKey, 'k1')
    let state = readProfiles()
    expect(state.profiles).toEqual([{ id: LOCAL_PROFILE_ID, name: '', url: '', apiKey: 'k1' }])

    storage.clear()
    storage.set(STORAGE_KEYS.backendUrl, 'http://127.0.0.1:8000')
    state = readProfiles()
    expect(state.profiles).toEqual([{ id: LOCAL_PROFILE_ID, name: '', url: '', apiKey: null }])

    storage.clear()
    storage.set(STORAGE_KEYS.backendUrl, 'http://localhost:8123/')
    state = readProfiles()
    expect(state.profiles[0]!.url).toBe('http://localhost:8123')
    expect(state.activeId).toBe(LOCAL_PROFILE_ID)
  })

  it('migrates once; a corrupted value falls back to the built-in profile', () => {
    storage.set(STORAGE_KEYS.backendUrl, 'http://nas:8000')
    const first = readProfiles()
    storage.set(STORAGE_KEYS.backendUrl, 'http://other:8000') // written by an old tab: ignored
    expect(readProfiles()).toEqual(first)
    storage.set(STORAGE_KEYS.profiles, '{nope')
    expect(readProfiles().profiles.map((p) => p.id)).toEqual([LOCAL_PROFILE_ID])
  })
})

describe('profiles store', () => {
  it('keeps one key per profile and sends the active one on every transport', async () => {
    const profiles = useProfilesStore()
    await profiles.update(LOCAL_PROFILE_ID, { apiKey: 'local-key' })
    const nas = profiles.add({ name: ' NAS ', url: 'nas:8000', apiKey: 'nas-key' })
    expect(nas).toMatchObject({ name: 'NAS', url: 'http://nas:8000', apiKey: 'nas-key' })
    expect(profiles.activeId).toBe(LOCAL_PROFILE_ID)
    expect(authHeaders()).toEqual({ 'X-API-Key': 'local-key' })

    expect(await profiles.switchTo(nas.id)).toBe(true)
    expect(getBackendUrl()).toBe('http://nas:8000')
    expect(authHeaders()).toEqual({ 'X-API-Key': 'nas-key' })
    calls.length = 0
    await useTermSetsStore().load()
    const termCall = calls.find((c) => c.url.endsWith('/api/term-sets'))
    expect(termCall).toMatchObject({ url: 'http://nas:8000/api/term-sets', key: 'nas-key' })

    expect(await profiles.switchTo(LOCAL_PROFILE_ID)).toBe(true)
    expect(authHeaders()).toEqual({ 'X-API-Key': 'local-key' })
    // The keys live in the stored profiles only.
    expect(readProfiles().profiles.map((p) => p.apiKey)).toEqual(['local-key', 'nas-key'])
  })

  it('validates URLs like before: http(s) only, a remote one cannot be empty', async () => {
    const profiles = useProfilesStore()
    expect(() => profiles.add({ name: 'x', url: 'ftp://nas', apiKey: null })).toThrow(/scheme/i)
    expect(() => profiles.add({ name: 'x', url: '', apiKey: null })).toThrow()
    const nas = profiles.add({ name: '', url: 'nas:8000', apiKey: null })
    expect(nas.name).toBe('nas:8000') // unnamed: the host
    await expect(profiles.update(nas.id, { url: '' })).rejects.toThrow()
    expect(profiles.find(nas.id)!.url).toBe('http://nas:8000')
  })

  it('a key would go to another origin: keyAtRisk, and clearApiKey drops it', async () => {
    const profiles = useProfilesStore()
    const nas = profiles.add({ name: 'NAS', url: 'http://nas:8000', apiKey: 'k' })
    expect(profiles.keyAtRisk(nas.id, 'http://nas:8000/')).toBe(false)
    expect(profiles.keyAtRisk(nas.id, 'http://other:8000')).toBe(true)
    expect(profiles.keyAtRisk(LOCAL_PROFILE_ID, 'http://other:8000')).toBe(false) // no key
    await profiles.update(nas.id, { url: 'http://other:8000' }, { clearApiKey: true })
    expect(profiles.find(nas.id)).toMatchObject({ url: 'http://other:8000', apiKey: null })
  })

  it('switching resets every per-backend store and re-mounts the view', async () => {
    const profiles = useProfilesStore()
    const nas = profiles.add({ name: 'NAS', url: 'http://nas:8000', apiKey: null })
    const jobs = useJobsStore()
    const termSets = useTermSetsStore()
    const uploads = useUploadsStore()
    const health = useHealthStore()
    const hardware = useHardwareStore()
    jobs.items = [{ id: 'j1' } as never]
    termSets.items = [{ id: 'ts1' } as never]
    uploads.ready = [{ upload: { id: 'u1' } } as never]
    uploads.jobForm.termSetIds = ['ts1']
    uploads.jobForm.device = 'rocm'
    uploads.rememberDir('/local/dir')
    health.markUnauthorized()
    hardware.info = { default: 'cpu', devices: [] }
    const epoch = profiles.epoch

    expect(await profiles.switchTo(nas.id)).toBe(true)
    expect(jobs.items).toEqual([])
    expect(termSets.items).toEqual([])
    expect(uploads.ready).toEqual([])
    expect(uploads.jobForm.termSetIds).toEqual([])
    expect(uploads.jobForm.device).toBeNull()
    expect(uploads.targetDir).toBe('') // the NAS has its own remembered dir
    expect(health.unauthorized).toBe(false)
    expect(hardware.info).toBeNull()
    expect(profiles.epoch).toBe(epoch + 1)
    await flushPromises()
    // Reloaded from the new backend.
    expect(calls.some((c) => c.url === 'http://nas:8000/api/health')).toBe(true)
    expect(calls.some((c) => c.url === 'http://nas:8000/api/settings')).toBe(true)
    expect(calls.some((c) => c.url === 'http://nas:8000/api/system/hardware')).toBe(true)

    await profiles.switchTo(LOCAL_PROFILE_ID)
    expect(uploads.targetDir).toBe('/local/dir')
  })

  it('flushes transcript edits first and asks about what could not be saved', async () => {
    const tr = {
      id: 'T1', job_id: 'J1', provider: 'cloud', duration: 2, language: 'zh',
      created_at: '2026-10-30T09:00:00Z', updated_at: '2026-10-30T09:00:00Z',
      speakers: [], segments: [{ id: 's0', start: 0, end: 1, speaker_id: 'SPEAKER_00', text: 'old' }],
      original: [], corrections: [], summary: null, warnings: [],
    }
    let patchStatus = 200
    respond = (method, url) => {
      if (url.endsWith('/api/transcripts/T1') && method === 'GET') return json(tr)
      if (url.endsWith('/api/transcripts/T1') && method === 'PATCH') {
        return patchStatus === 200 ? json({ ...tr, updated_at: '2026-10-30T09:00:01Z' }) : json({ detail: 'down' }, patchStatus)
      }
      if (url.endsWith('/api/jobs/J1')) return json({ id: 'J1', upload_id: null, options: {} })
      return json({})
    }
    const profiles = useProfilesStore()
    await profiles.update(LOCAL_PROFILE_ID, { url: 'http://127.0.0.1:8000' }) // Node needs absolute URLs
    const nas = profiles.add({ name: 'NAS', url: 'http://nas:8000', apiKey: null })
    const transcript = useTranscriptStore()
    await transcript.load('T1')

    // Saved by the flush: no question asked, the PATCH went to the old backend.
    transcript.editText('s0', 'saved')
    const confirm = vi.fn(async () => false)
    expect(await profiles.switchTo(nas.id, { confirm })).toBe(true)
    expect(confirm).not.toHaveBeenCalled()
    const patch = calls.find((c) => c.method === 'PATCH')!
    expect(patch.url).toBe('http://127.0.0.1:8000/api/transcripts/T1')

    // Back, edit, and the save fails: the user is asked; declining keeps everything.
    await profiles.switchTo(LOCAL_PROFILE_ID)
    await transcript.load('T1')
    patchStatus = 503
    transcript.editText('s0', 'unsaved')
    expect(await profiles.switchTo(nas.id, { confirm })).toBe(false)
    expect(confirm).toHaveBeenCalledWith('unsavedEdits')
    expect(profiles.activeId).toBe(LOCAL_PROFILE_ID)
    expect(transcript.transcript?.segments[0]!.text).toBe('unsaved')

    // Accepting discards the edit without sending it to the new backend.
    calls.length = 0
    expect(await profiles.switchTo(nas.id, { confirm: async () => true })).toBe(true)
    expect(transcript.transcript).toBeNull()
    expect(transcript.hasUnsavedChanges).toBe(false)
    await flushPromises()
    expect(calls.filter((c) => c.method === 'PATCH' && c.url.startsWith('http://nas:8000'))).toEqual([])
  })

  it('asks before aborting a running upload', async () => {
    const profiles = useProfilesStore()
    const nas = profiles.add({ name: 'NAS', url: 'http://nas:8000', apiKey: null })
    const uploads = useUploadsStore()
    uploads.uploading = true
    const confirm = vi.fn(async () => false)
    expect(await profiles.switchTo(nas.id, { confirm })).toBe(false)
    expect(confirm).toHaveBeenCalledWith('uploadRunning')
    expect(await profiles.switchTo(nas.id)).toBe(false) // no confirm callback: never silently
    expect(await profiles.switchTo(nas.id, { confirm: async () => true })).toBe(true)
    expect(uploads.uploading).toBe(false)
  })

  it('removes remote profiles, never the built-in or the active one', async () => {
    const profiles = useProfilesStore()
    const nas = profiles.add({ name: 'NAS', url: 'http://nas:8000', apiKey: 'k' })
    expect(() => profiles.remove(LOCAL_PROFILE_ID)).toThrow()
    await profiles.switchTo(nas.id)
    expect(() => profiles.remove(nas.id)).toThrow(/switch/)
    await profiles.switchTo(LOCAL_PROFILE_ID)
    profiles.remove(nas.id)
    expect(profiles.profiles.map((p) => p.id)).toEqual([LOCAL_PROFILE_ID])
  })

  it('editing the active profile to another origin is a backend change', async () => {
    const profiles = useProfilesStore()
    const nas = profiles.add({ name: 'NAS', url: 'http://nas:8000', apiKey: null })
    await profiles.switchTo(nas.id)
    const epoch = profiles.epoch
    expect(profiles.changesBackend(nas.id, 'http://nas:8000/')).toBe(false)
    expect(profiles.changesBackend(nas.id, 'http://nas2:8000')).toBe(true)
    await profiles.update(nas.id, { name: 'NAS 2' })
    expect(profiles.epoch).toBe(epoch)
    await profiles.update(nas.id, { url: 'http://nas2:8000' })
    expect(profiles.epoch).toBe(epoch + 1)
    expect(getBackendUrl()).toBe('http://nas2:8000')
  })

  it('test(): health, then the key against an authenticated endpoint', async () => {
    respond = (_m, url) => {
      if (url.endsWith('/api/health')) return json({ status: 'ok', version: '1.2', decoder: 'pyav', ffmpeg_version: null, api_key_required: true })
      if (url.endsWith('/api/term-sets')) return json({ detail: 'no' }, 401)
      return json({})
    }
    const profiles = useProfilesStore()
    const result = await profiles.test('nas:8000', 'bad')
    expect(result.health.version).toBe('1.2')
    expect(result.keyAccepted).toBe(false)
    expect(calls.at(-1)).toMatchObject({ url: 'http://nas:8000/api/term-sets', key: 'bad' })
    await expect(profiles.test('ftp://nas', null)).rejects.toThrow()
  })
})

describe('profiles store on desktop', () => {
  let fake: FakeDesktop

  beforeEach(() => {
    stubWindow(true)
    fake = createFakeDesktop()
    setDesktopPlatformForTests(fake)
    setActivePinia(createPinia())
  })

  it('pushes the active profile to Rust and keeps the recording settings per profile', async () => {
    const profiles = useProfilesStore()
    const recorder = useRecorderStore()
    await recorder.init()
    await recorder.saveConfig({ targetDir: 'D:\\rec', termSetIds: ['local-ts'] })
    expect(fake.state.config).toMatchObject({ baseUrl: 'http://127.0.0.1:8000', targetDir: 'D:\\rec' })

    const nas = profiles.add({ name: 'NAS', url: 'http://nas:8000', apiKey: 'nas-key' })
    expect(await profiles.switchTo(nas.id)).toBe(true)
    expect(fake.state.config).toMatchObject({ baseUrl: 'http://nas:8000', termSetIds: [] })
    expect(fake.state.storedKey).toBe('nas-key')
    await recorder.saveConfig({ targetDir: '/volume1/rec', termSetIds: ['nas-ts'] })

    expect(await profiles.switchTo(LOCAL_PROFILE_ID)).toBe(true)
    expect(fake.state.config).toMatchObject({
      baseUrl: 'http://127.0.0.1:8000',
      targetDir: 'D:\\rec',
      termSetIds: ['local-ts'],
    })
    expect(fake.state.storedKey).toBeNull()
    await profiles.switchTo(nas.id)
    expect(fake.state.config).toMatchObject({ targetDir: '/volume1/rec', termSetIds: ['nas-ts'] })
    expect(profiles.desktopError).toBeNull()
  })

  it('the built-in profile follows the running sidecar', async () => {
    const profiles = useProfilesStore()
    profiles.init()
    await flushPromises()
    expect(profiles.activeUrl).toBe('http://127.0.0.1:8000')
    fake.emitSidecar({ state: 'running', pid: 7, url: 'http://127.0.0.1:8123', error: null })
    await flushPromises()
    expect(profiles.activeUrl).toBe('http://127.0.0.1:8123')
    expect(getBackendUrl()).toBe('http://127.0.0.1:8123')
    expect(fake.state.config.baseUrl).toBe('http://127.0.0.1:8123')
    fake.emitSidecar({ state: 'stopped', pid: null, url: null, error: null })
    await flushPromises()
    expect(getBackendUrl()).toBe('http://127.0.0.1:8000')
  })
})
