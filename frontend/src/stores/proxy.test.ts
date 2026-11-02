/**
 * Desktop proxy setting: URL validation, cleaning, load / save / test against the fake platform
 * (credentials masked, masked URL keeps them), an older core without the commands.
 */
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setDesktopPlatformForTests } from '@/platform'
import { createFakeDesktop, maskProxyUrl, type FakeDesktop } from '@/platform/testing'

import {
  cleanNoProxy,
  cleanProxySettings,
  DEFAULT_NO_PROXY,
  proxyUrlError,
  sameProxySettings,
  useProxyStore,
} from './proxy'

let fake: FakeDesktop

beforeEach(() => {
  vi.stubGlobal('window', { __TAURI_INTERNALS__: {} })
  fake = createFakeDesktop()
  setDesktopPlatformForTests(fake)
  setActivePinia(createPinia())
})

afterEach(() => {
  setDesktopPlatformForTests(null)
  vi.unstubAllGlobals()
})

describe('proxy helpers', () => {
  it('validates manual proxy URLs', () => {
    expect(proxyUrlError('http://127.0.0.1:7890')).toBeNull()
    expect(proxyUrlError(' https://proxy.corp:8443 ')).toBeNull()
    expect(proxyUrlError('socks5://127.0.0.1:1080')).toBeNull()
    expect(proxyUrlError('http://user:***@proxy.corp:3128')).toBeNull() // the core's masked form
    expect(proxyUrlError('http://proxy.corp:3128/')).toBeNull()
    expect(proxyUrlError('')).toBe('required')
    expect(proxyUrlError(null)).toBe('required')
    expect(proxyUrlError('127.0.0.1:7890')).toBe('invalid')
    expect(proxyUrlError('ftp://proxy:21')).toBe('scheme')
    expect(proxyUrlError('socks4://proxy:1080')).toBe('scheme')
    expect(proxyUrlError('http://proxy:3128/path')).toBe('invalid')
    expect(proxyUrlError('http://proxy:99999')).toBe('invalid')
    expect(proxyUrlError('http://pro xy:3128')).toBe('invalid')
  })

  it('cleans settings: URL only in manual mode, tidy no-proxy list', () => {
    expect(cleanNoProxy(' a, ,b ,c,')).toBe('a,b,c')
    expect(cleanProxySettings({ mode: 'system', url: 'http://x:1', noProxy: '' })).toEqual({
      mode: 'system',
      url: null,
      noProxy: DEFAULT_NO_PROXY,
    })
    expect(cleanProxySettings({ mode: 'manual', url: ' http://x:1 ', noProxy: 'a, b' })).toEqual({
      mode: 'manual',
      url: 'http://x:1',
      noProxy: 'a,b',
    })
    expect(
      sameProxySettings(
        { mode: 'system', url: '', noProxy: 'a,b' },
        { mode: 'system', url: null, noProxy: ' a , b ' },
      ),
    ).toBe(true)
  })

  it('masks credentials like the core', () => {
    expect(maskProxyUrl('http://me:s3cret@proxy:3128')).toBe('http://me:***@proxy:3128')
    expect(maskProxyUrl('http://proxy:3128')).toBe('http://proxy:3128')
  })
})

describe('proxy store', () => {
  it('is inert in the web build', async () => {
    vi.stubGlobal('window', {})
    setActivePinia(createPinia())
    const store = useProxyStore()
    await store.load()
    expect(store.supported).toBe(false)
    expect(fake.calls).toEqual([])
  })

  it('hides itself with an older core', async () => {
    fake.state.missing.add('network_get_proxy')
    const store = useProxyStore()
    await store.load()
    expect(store.supported).toBe(false)
    expect(store.ready).toBe(false)
  })

  it('loads settings + the effective proxy (system by default)', async () => {
    const store = useProxyStore()
    await store.load()
    expect(store.ready).toBe(true)
    expect(store.settings).toEqual({ mode: 'system', url: null, noProxy: DEFAULT_NO_PROXY })
    expect(store.effective).toMatchObject({ source: 'system', url: 'http://127.0.0.1:7890' })
  })

  it('saves manual with credentials; reads them back masked; the masked URL keeps them', async () => {
    const store = useProxyStore()
    await store.load()
    await store.save({ mode: 'manual', url: 'http://me:s3cret@proxy.corp:3128', noProxy: 'localhost, 10.*' })
    expect(fake.calls.at(-1)).toEqual([
      'network_set_proxy',
      { settings: { mode: 'manual', url: 'http://me:s3cret@proxy.corp:3128', noProxy: 'localhost,10.*' } },
    ])
    expect(store.settings.url).toBe('http://me:***@proxy.corp:3128')
    expect(store.effective).toMatchObject({ source: 'manual', url: 'http://me:***@proxy.corp:3128' })
    // Change only the no-proxy list: the masked URL goes back and the stored password stays.
    await store.save({ ...store.settings, noProxy: 'localhost' })
    expect(fake.state.proxy.url).toBe('http://me:s3cret@proxy.corp:3128')
    await store.save({ mode: 'none', url: 'http://ignored:1', noProxy: '' })
    expect(fake.state.proxy).toEqual({ mode: 'none', url: null, noProxy: DEFAULT_NO_PROXY })
    expect(store.effective?.source).toBe('none')
  })

  it('tests the unsaved draft and reports IPC errors', async () => {
    const store = useProxyStore()
    await store.load()
    const r = await store.test({ mode: 'manual', url: 'socks5://127.0.0.1:1080', noProxy: '' })
    expect(r.ok).toBe(true)
    expect(fake.calls.at(-1)).toEqual([
      'network_test_proxy',
      { settings: { mode: 'manual', url: 'socks5://127.0.0.1:1080', noProxy: DEFAULT_NO_PROXY } },
    ])
    fake.state.failNext = 'timeout'
    await expect(store.save({ mode: 'system', url: null, noProxy: '' })).rejects.toThrow('timeout')
    expect(store.saving).toBe(false)
  })
})
