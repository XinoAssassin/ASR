import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setDesktopPlatformForTests } from '@/platform'
import { createFakeDesktop, type FakeDesktop } from '@/platform/testing'

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
  getBackendUrlOverride: () => browser.backendUrl,
  getApiKey: () => browser.apiKey,
  setBackendUrl: (url: string | null) => (browser.backendUrl = (url ?? '').replace(/\/+$/, '')),
  setApiKey: vi.fn(),
  getJob: vi.fn(),
  getSettings: vi.fn(),
  getHealth: vi.fn(async () => ({ status: 'ok', version: 'test' })),
  updateSettings: vi.fn(),
  testCloud: vi.fn(),
  toApiError: (e: unknown) => e,
}))

const { cleanSidecarConfig, syncPortArg, useSidecarStore } = await import('./sidecar')

let fake: FakeDesktop

beforeEach(() => {
  browser.storage.clear()
  browser.backendUrl = ''
  vi.stubGlobal('window', { __TAURI_INTERNALS__: {} })
  fake = createFakeDesktop()
  setDesktopPlatformForTests(fake)
  setActivePinia(createPinia())
})

afterEach(() => {
  setDesktopPlatformForTests(null)
  vi.unstubAllGlobals()
})

describe('sidecar helpers', () => {
  it('keeps --port in step with the port field', () => {
    expect(syncPortArg(['serve', '--port', '8000'], 8123)).toEqual(['serve', '--port', '8123'])
    expect(syncPortArg(['serve', '--port=8000', '-v'], 9000)).toEqual(['serve', '--port=9000', '-v'])
    expect(syncPortArg(['serve', '--port'], 9000)).toEqual(['serve', '--port', '9000'])
    expect(syncPortArg(['serve'], 9000)).toEqual(['serve'])
  })

  it('cleans the form', () => {
    expect(
      cleanSidecarConfig({ enabled: true, command: '  C:\\asr.exe ', args: ['serve', ' ', ''], cwd: '  ', port: 70000 }),
    ).toEqual({ enabled: true, command: 'C:\\asr.exe', args: ['serve'], cwd: null, port: 8000 })
  })
})

describe('sidecar store', () => {
  it('is inert in the web build', async () => {
    vi.stubGlobal('window', {})
    const store = useSidecarStore()
    await store.init()
    expect(fake.calls).toEqual([])
  })

  it('loads config + status and follows sidecar://status', async () => {
    const store = useSidecarStore()
    await store.init()
    expect(store.config?.args).toEqual(['serve', '--port', '8000'])
    expect(store.status.state).toBe('stopped')
    fake.emitSidecar({ state: 'starting', pid: 12, url: null, error: null })
    expect(store.status.state).toBe('starting')
  })

  it('saves, starts, uses the backend (SPA + Rust), stops', async () => {
    const store = useSidecarStore()
    await store.init()
    await store.saveConfig({ ...store.config!, command: 'D:\\ASR\\asr.exe', port: 8123, args: ['serve', '--port', '8123'] })
    expect(fake.state.sidecarConfig.command).toBe('D:\\ASR\\asr.exe')
    const st = await store.start()
    expect(st).toMatchObject({ state: 'running', url: 'http://127.0.0.1:8123' })
    const url = await store.useAsBackend()
    expect(url).toBe('http://127.0.0.1:8123')
    expect(browser.backendUrl).toBe('http://127.0.0.1:8123')
    expect(fake.state.config.baseUrl).toBe('http://127.0.0.1:8123') // pushed to Rust
    await store.stop()
    expect(store.status.state).toBe('stopped')
    await expect(store.useAsBackend()).rejects.toThrow(/not running/)
  })

  it('reports an older core without sidecar commands', async () => {
    fake.state.missing.add('sidecar_get_config')
    const store = useSidecarStore()
    await store.init()
    expect(store.error).toMatch(/not found/)
    expect(store.config).toBeNull()
  })

  it('does not leak the status listener when init fails, so retries do not stack', async () => {
    fake.state.missing.add('sidecar_get_config')
    const store = useSidecarStore()
    await store.init()
    expect(fake.listenerCount()).toBe(0)
    await store.init()
    expect(fake.listenerCount()).toBe(0)
    fake.state.missing.clear()
    await store.init()
    expect(fake.listenerCount()).toBe(1)
    expect(store.error).toBeNull()
  })
})
