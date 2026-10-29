import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'

import { setDesktopPlatformForTests } from '@/platform'
import { createFakeDesktop, makeRecording, type FakeDesktop } from '@/platform/testing'

const browser = vi.hoisted(() => ({
  storage: new Map<string, string>(),
  backendUrl: '',
  apiKey: null as string | null,
}))

vi.mock('@/api', () => ({
  readStorage: (k: string) => browser.storage.get(k) ?? null,
  writeStorage: (k: string, v: string | null) =>
    v ? browser.storage.set(k, v) : browser.storage.delete(k),
  getBackendUrl: () => browser.backendUrl,
  getApiKey: () => browser.apiKey,
  getJob: vi.fn(async (id: string) => ({ id, transcript_id: `tr-${id}` })),
}))

const { DESKTOP_DEFAULT_BACKEND, SET_OPTIONS_DEBOUNCE_MS, normalizeStatus, useRecorderStore } =
  await import('./recorder')

let fake: FakeDesktop

beforeEach(() => {
  browser.storage.clear()
  browser.backendUrl = ''
  browser.apiKey = null
  vi.stubGlobal('window', { __TAURI_INTERNALS__: {} })
  fake = createFakeDesktop()
  setDesktopPlatformForTests(fake)
  setActivePinia(createPinia())
})

afterEach(() => {
  setDesktopPlatformForTests(null)
  vi.unstubAllGlobals()
})

const names = () => fake.calls.map(([name]) => name)

describe('recorder store', () => {
  it('is inert in the web build', async () => {
    vi.stubGlobal('window', {})
    const store = useRecorderStore()
    expect(store.available).toBe(false)
    await store.init()
    expect(fake.calls).toEqual([])
    expect(store.canStart).toBe(false)
  })

  it('loads everything on init and hands Rust the browser connection', async () => {
    browser.backendUrl = 'http://gpu:8000'
    browser.apiKey = 'secret'
    const store = useRecorderStore()
    await store.init()
    await store.init() // idempotent
    expect(store.initialized).toBe(true)
    expect(store.devices.outputs).toHaveLength(2)
    expect(store.recordings.map((r) => r.id)).toEqual(['2026-10-28_09-00-00'])
    expect(fake.listenerCount()).toBe(2)
    expect(names().filter((n) => n === 'recordings_list')).toHaveLength(1)
    // Rust had no URL/key: the browser settings were pushed, the key comes back masked.
    expect(fake.state.config.baseUrl).toBe('http://gpu:8000')
    expect(fake.state.storedKey).toBe('secret')
    expect(store.config?.apiKey).toBe('***')
    expect(store.keySet).toBe(true)
  })

  it('does not push again when Rust is in sync', async () => {
    fake.state.config.baseUrl = DESKTOP_DEFAULT_BACKEND
    const store = useRecorderStore()
    await store.init()
    expect(names()).not.toContain('backend_set_config')
  })

  it('starts with the chosen options and follows status events', async () => {
    const store = useRecorderStore()
    await store.init()
    store.options.mic = false
    store.options.outputDevice = 'out-2'
    await store.start()
    expect(fake.calls.find(([n]) => n === 'recorder_start')![1]).toEqual({
      options: {
        system: true,
        mic: false,
        outputDevice: 'out-2',
        inputDevice: null,
        outDir: null,
        processId: null,
      },
    })
    expect(store.recording).toBe(true)
    fake.emitStatus({
      state: 'recording',
      elapsed: 3.2,
      levels: { system: 0.4, mic: 0 },
      file: 'x.partial.wav',
      driftMs: 1.5,
      warnings: ['system audio silent for 60 s'],
      sources: { system: true, mic: false },
      processId: null,
    })
    expect(store.status.elapsed).toBe(3.2)
    expect(store.status.levels.system).toBe(0.4)
    expect(store.status.warnings).toHaveLength(1)
  })

  it('persists start options and drops devices that disappeared', async () => {
    browser.storage.set(
      'asr.recorder.startOptions',
      JSON.stringify({ system: false, mic: true, outputDevice: 'gone', inputDevice: 'in-1', outDir: 'D:\\rec' }),
    )
    const store = useRecorderStore()
    await store.init()
    expect(store.options).toEqual({
      system: false,
      mic: true,
      outputDevice: null,
      inputDevice: 'in-1',
      outDir: 'D:\\rec',
    })
    store.options.system = true
    await nextTick()
    expect(JSON.parse(browser.storage.get('asr.recorder.startOptions')!).system).toBe(true)
  })

  it('cannot start with both sources off', async () => {
    const store = useRecorderStore()
    await store.init()
    store.options.system = false
    store.options.mic = false
    expect(store.canStart).toBe(false)
    await store.start()
    expect(names()).not.toContain('recorder_start')
  })

  it('stop adds the new recording first; change events update it in place', async () => {
    const store = useRecorderStore()
    await store.init()
    await store.start()
    const rec = await store.stop()
    expect(rec?.id).toBe('2026-10-29_10-00-00')
    expect(store.recordings.map((r) => r.id)).toEqual(['2026-10-29_10-00-00', '2026-10-28_09-00-00'])
    expect(store.recording).toBe(false)

    fake.emitRecording(makeRecording('2026-10-29_10-00-00', { state: 'uploading', attempts: 1 }))
    expect(store.recordings).toHaveLength(2)
    expect(store.recordings[0]!.upload.state).toBe('uploading')
  })

  it('surfaces IPC errors (Rust strings)', async () => {
    const store = useRecorderStore()
    await store.init()
    fake.state.failNext = 'no loopback device'
    await store.start()
    expect(store.error).toBe('no loopback device')
    expect(store.recording).toBe(false)

    fake.state.failNext = 'offline'
    await expect(store.upload('2026-10-28_09-00-00')).rejects.toThrow('offline')
  })

  it('saveConfig keeps the connection from the browser and merges the rest', async () => {
    browser.apiKey = 'k1'
    const store = useRecorderStore()
    await store.init()
    const jobOptions = { provider: 'local' as const }
    await store.saveConfig({ targetDir: '/data/rec', jobOptions, provider: 'local' })
    expect(fake.state.config).toEqual({
      baseUrl: DESKTOP_DEFAULT_BACKEND,
      apiKey: 'k1',
      targetDir: '/data/rec',
      autoUpload: true,
      jobOptions,
      provider: 'local',
      termSetIds: [],
      extraTerms: [],
    })
    await store.saveConfig({ termSetIds: ['ts1'], extraTerms: [{ term: 'PyTorch', weight: 3 }] })
    expect(fake.state.config).toMatchObject({ targetDir: '/data/rec', termSetIds: ['ts1'] })
    // Key cleared in the browser → cleared in Rust on the next sync.
    browser.apiKey = null
    await store.syncBackend()
    expect(fake.state.storedKey).toBeNull()
    expect(fake.state.config.targetDir).toBe('/data/rec')
  })

  it('resolves transcript links for finished jobs', async () => {
    const store = useRecorderStore()
    expect(await store.lookupTranscript('job-1')).toBe('tr-job-1')
    expect(store.transcriptIds).toEqual({ 'job-1': 'tr-job-1' })
  })

  it('dispose unsubscribes', async () => {
    const store = useRecorderStore()
    await store.init()
    store.dispose()
    expect(fake.listenerCount()).toBe(0)
  })

  it('captures one application by pid and shows it while recording (M10)', async () => {
    const store = useRecorderStore()
    await store.init()
    expect(names()).toContain('recorder_processes')
    expect(store.processes.map((p) => p.pid)).toEqual([4242, 5150])
    store.sourceMode = 'process'
    expect(store.needsProcess).toBe(true)
    expect(store.canStart).toBe(false)
    store.selectProcess(4242)
    expect(store.processName).toBe('Teams.exe')
    expect(store.canStart).toBe(true)
    await store.start()
    expect(fake.calls.find(([n]) => n === 'recorder_start')![1]).toMatchObject({
      options: { system: true, processId: 4242 },
    })
    expect(store.status.processId).toBe(4242)
    expect(store.capturedProcess?.name).toBe('Teams.exe')
    await nextTick()
    expect(JSON.parse(browser.storage.get('asr.recorder.systemSource')!)).toEqual({
      mode: 'process',
      processName: 'Teams.exe',
    })
  })

  it('sends no processId for the whole device or with system audio off', async () => {
    const store = useRecorderStore()
    await store.init()
    store.selectProcess(4242)
    expect(store.startOptions.processId).toBeNull() // mode is still 'device'
    store.sourceMode = 'process'
    store.options.system = false
    expect(store.startOptions.processId).toBeNull()
    expect(store.needsProcess).toBe(false)
  })

  it('re-matches a remembered app by name after a restart (new pid)', async () => {
    browser.storage.set(
      'asr.recorder.systemSource',
      JSON.stringify({ mode: 'process', processName: 'chrome.exe' }),
    )
    const store = useRecorderStore()
    await store.init()
    expect(store.sourceMode).toBe('process')
    expect(store.processId).toBe(5150)
    expect(fake.calls.some(([n]) => n === 'recorder_set_options')).toBe(false) // init: not pushed
    fake.state.processes = [{ pid: 9999, name: 'chrome.exe', title: 'Meet' }]
    await store.refreshProcesses()
    expect(store.processId).toBe(9999)
    fake.state.processes = []
    await store.refreshProcesses()
    expect(store.processId).toBeNull()
    expect(store.processName).toBe('chrome.exe') // kept for the next refresh
  })

  it('reports actual sources when the core drops a missing mic', async () => {
    fake.state.noMic = true
    const store = useRecorderStore()
    await store.init()
    await store.start()
    expect(store.status.sources).toEqual({ system: true, mic: false })
    expect(store.status.warnings[0]).toMatch(/microphone/)
  })

  it('keeps working with an older core (no recorder_processes, no sources)', async () => {
    fake.state.missing.add('recorder_processes')
    const store = useRecorderStore()
    await store.init()
    expect(store.error).toBeNull()
    expect(store.processesError).toMatch(/not found/)
    const old = { state: 'recording', elapsed: 1, levels: { system: 0, mic: 0 }, file: null, driftMs: null, warnings: [] }
    const s = normalizeStatus(old as never, { ...store.options, mic: false })
    expect(s.sources).toEqual({ system: true, mic: false })
    expect(s.processId).toBeNull()
  })

  describe('recorder_set_options (tray / hotkey defaults)', () => {
    afterEach(() => vi.useRealTimers())

    const pushes = () => fake.calls.filter(([n]) => n === 'recorder_set_options')

    it('pushes edits while idle, debounced, but not the restored options at init', async () => {
      const store = useRecorderStore()
      await store.init()
      vi.useFakeTimers()
      await nextTick()
      expect(pushes()).toHaveLength(0)
      store.options.mic = false
      await nextTick()
      store.options.outputDevice = 'out-2'
      await nextTick()
      vi.advanceTimersByTime(SET_OPTIONS_DEBOUNCE_MS - 1)
      expect(pushes()).toHaveLength(0)
      await vi.advanceTimersByTimeAsync(1)
      expect(pushes()).toHaveLength(1)
      expect(fake.state.lastOptions).toEqual({
        system: true,
        mic: false,
        outputDevice: 'out-2',
        inputDevice: null,
        outDir: null,
        processId: null,
      })
      // Choosing an app sends its pid.
      store.sourceMode = 'process'
      store.selectProcess(4242)
      await nextTick()
      await vi.advanceTimersByTimeAsync(SET_OPTIONS_DEBOUNCE_MS)
      expect(pushes()).toHaveLength(2)
      expect(fake.state.lastOptions?.processId).toBe(4242)
    })

    it('does not push while recording; start cancels a pending push', async () => {
      const store = useRecorderStore()
      await store.init()
      vi.useFakeTimers()
      store.options.mic = false
      await nextTick()
      await store.start()
      await vi.advanceTimersByTimeAsync(SET_OPTIONS_DEBOUNCE_MS * 2)
      expect(pushes()).toHaveLength(0)
      expect(fake.state.lastOptions?.mic).toBe(false) // stored by recorder_start
      store.options.outDir = 'D:\\rec' // locked in the UI; even so, nothing is pushed
      await nextTick()
      await vi.advanceTimersByTimeAsync(SET_OPTIONS_DEBOUNCE_MS)
      expect(pushes()).toHaveLength(0)
    })

    it('stops calling an older core that lacks the command', async () => {
      fake.state.missing.add('recorder_set_options')
      const store = useRecorderStore()
      await store.init()
      vi.useFakeTimers()
      store.options.mic = false
      await nextTick()
      await vi.advanceTimersByTimeAsync(SET_OPTIONS_DEBOUNCE_MS)
      expect(pushes()).toHaveLength(1)
      expect(store.error).toBeNull()
      store.options.mic = true
      await nextTick()
      await vi.advanceTimersByTimeAsync(SET_OPTIONS_DEBOUNCE_MS)
      expect(pushes()).toHaveLength(1)
    })
  })
})
