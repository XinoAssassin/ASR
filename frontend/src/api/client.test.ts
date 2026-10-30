import { afterEach, describe, expect, it, vi } from 'vitest'

function stubWindow(extra: Record<string, unknown> = {}, stored: Record<string, string> = {}) {
  const data = new Map(Object.entries(stored))
  vi.stubGlobal('window', {
    localStorage: {
      getItem: (k: string) => data.get(k) ?? null,
      setItem: (k: string, v: string) => data.set(k, v),
      removeItem: (k: string) => data.delete(k),
    },
    ...extra,
  })
}

stubWindow() // client.ts builds its typed client at import time
const {
  backendOrigin,
  getBackendUrl,
  getBackendUrlOverride,
  getDefaultBackendUrl,
  normalizeBackendUrl,
  notifyAuthorized,
  onAuthorized,
  setBackendUrl,
  STORAGE_KEYS,
} = await import('./client')
const { BackendUrlError } = await import('./errors')

afterEach(() => vi.unstubAllGlobals())

describe('backend URL', () => {
  it('web: override → config.js → same origin', () => {
    stubWindow()
    expect(getBackendUrl()).toBe('')
    expect(getDefaultBackendUrl()).toBe('')
    stubWindow({ __ASR_CONFIG__: { backendUrl: 'http://gpu:8000/' } })
    expect(getBackendUrl()).toBe('http://gpu:8000')
    stubWindow(
      { __ASR_CONFIG__: { backendUrl: 'http://gpu:8000' } },
      { [STORAGE_KEYS.backendUrl]: 'http://other:9000' },
    )
    expect(getBackendUrl()).toBe('http://other:9000')
  })

  it('desktop: defaults to the local backend instead of the WebView origin', () => {
    stubWindow({ __TAURI_INTERNALS__: {} })
    expect(getBackendUrl()).toBe('http://127.0.0.1:8000')
    expect(getDefaultBackendUrl()).toBe('http://127.0.0.1:8000')
    stubWindow({ __TAURI_INTERNALS__: {}, __ASR_CONFIG__: { backendUrl: 'http://gpu:8000' } })
    expect(getBackendUrl()).toBe('http://gpu:8000')
    stubWindow({ __TAURI_INTERNALS__: {} }, { [STORAGE_KEYS.backendUrl]: 'http://other:9000' })
    expect(getBackendUrl()).toBe('http://other:9000')
  })
})

describe('backend URL validation', () => {
  it('adds http:// when the scheme is missing and strips trailing slashes', () => {
    expect(normalizeBackendUrl('gpu:8000')).toBe('http://gpu:8000')
    expect(normalizeBackendUrl(' 192.168.1.5:8000/ ')).toBe('http://192.168.1.5:8000')
    expect(normalizeBackendUrl('localhost')).toBe('http://localhost')
    expect(normalizeBackendUrl('https://asr.example.com/base/')).toBe('https://asr.example.com/base')
    expect(normalizeBackendUrl('HTTP://Gpu:8000')).toBe('HTTP://Gpu:8000')
    expect(normalizeBackendUrl('')).toBe('')
    expect(normalizeBackendUrl(null)).toBe('')
  })

  it('rejects other schemes and garbage with a typed error', () => {
    const reason = (input: string) => {
      try {
        normalizeBackendUrl(input)
        return null
      } catch (e) {
        expect(e).toBeInstanceOf(BackendUrlError)
        return (e as InstanceType<typeof BackendUrlError>).reason
      }
    }
    expect(reason('ftp://gpu:8000')).toBe('scheme')
    expect(reason('file:///etc/passwd')).toBe('scheme')
    expect(reason('http://')).toBe('invalid')
    expect(reason('gpu:99999')).toBe('invalid')
    expect(reason('two words')).toBe('invalid')
  })

  it('setBackendUrl stores the normalised URL and saves nothing when invalid', () => {
    stubWindow()
    setBackendUrl('gpu:8000')
    expect(getBackendUrlOverride()).toBe('http://gpu:8000')
    expect(getBackendUrl()).toBe('http://gpu:8000')
    expect(() => setBackendUrl('ftp://x')).toThrow(BackendUrlError)
    expect(getBackendUrl()).toBe('http://gpu:8000')
    setBackendUrl('')
    expect(getBackendUrlOverride()).toBe('')
  })

  it('a stored URL without scheme (saved by an older version) still works', () => {
    stubWindow({}, { [STORAGE_KEYS.backendUrl]: 'gpu:8000' })
    expect(getBackendUrl()).toBe('http://gpu:8000')
    stubWindow({}, { [STORAGE_KEYS.backendUrl]: 'javascript:alert(1)' })
    expect(getBackendUrl()).toBe('') // unusable: falls back to same origin
  })
})

describe('authorized notifications', () => {
  it('fire for authenticated endpoints only', () => {
    let n = 0
    const off = onAuthorized(() => n++)
    notifyAuthorized('http://gpu:8000/api/health')
    notifyAuthorized('/api/health')
    expect(n).toBe(0)
    notifyAuthorized('http://gpu:8000/api/jobs?page=1')
    notifyAuthorized()
    expect(n).toBe(2)
    off()
  })
})

describe('backendOrigin', () => {
  it('resolves the effective backend origin (empty = this page / config.js)', () => {
    stubWindow({ location: { href: 'http://spa.local:5173/settings' } })
    expect(backendOrigin('')).toBe('http://spa.local:5173')
    expect(backendOrigin('gpu:8000')).toBe('http://gpu:8000')
    expect(backendOrigin('https://asr.example.com/base/')).toBe('https://asr.example.com')
    stubWindow({ location: { href: 'http://spa.local/' }, __ASR_CONFIG__: { backendUrl: 'http://cfg:9000' } })
    expect(backendOrigin(null)).toBe('http://cfg:9000')
    expect(() => backendOrigin('ftp://x')).toThrow(BackendUrlError)
  })
})
