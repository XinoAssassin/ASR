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
const { getBackendUrl, getDefaultBackendUrl, STORAGE_KEYS } = await import('./client')

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
