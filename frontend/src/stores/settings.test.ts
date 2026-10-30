import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const browser = vi.hoisted(() => ({ backendUrl: '' }))

vi.mock('@/api', async () => {
  const errors = await import('@/api/errors')
  return {
    BackendUrlError: errors.BackendUrlError,
    getApiKey: () => null,
    getBackendUrlOverride: () => browser.backendUrl,
    setBackendUrl: (url: string) => {
      if (url.startsWith('ftp:')) throw new errors.BackendUrlError('scheme', url)
      browser.backendUrl = url
    },
    setApiKey: () => undefined,
    getHealth: vi.fn(async () => ({ status: 'ok' })),
    readStorage: () => null,
    writeStorage: () => undefined,
    toApiError: errors.toApiError,
  }
})

const { useHealthStore } = await import('./health')
const { useSettingsStore } = await import('./settings')

beforeEach(() => {
  browser.backendUrl = ''
  setActivePinia(createPinia())
})

describe('settings → health.unauthorized', () => {
  it('is cleared when the backend URL or the API key changes', () => {
    const health = useHealthStore()
    const settings = useSettingsStore()
    health.markUnauthorized()
    settings.setBackendUrl('http://other:8000')
    expect(health.unauthorized).toBe(false)
    health.markUnauthorized()
    settings.setApiKey('k')
    expect(health.unauthorized).toBe(false)
  })

  it('an invalid backend URL throws BackendUrlError and changes nothing', () => {
    const health = useHealthStore()
    const settings = useSettingsStore()
    health.markUnauthorized()
    expect(() => settings.setBackendUrl('ftp://x')).toThrow(/scheme/i)
    expect(settings.backendUrl).toBe('')
    expect(health.unauthorized).toBe(true)
  })
})
