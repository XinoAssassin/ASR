import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const browser = vi.hoisted(() => ({
  backendUrl: '',
  apiKey: null as string | null,
  listLocalModels: vi.fn(),
  downloadLocalModel: vi.fn(),
}))

vi.mock('@/api', async () => {
  const errors = await import('@/api/errors')
  return {
    BackendUrlError: errors.BackendUrlError,
    getApiKey: () => browser.apiKey,
    backendOrigin: (url: string | null) => new URL(url || 'http://page.local').origin,
    getBackendUrlOverride: () => browser.backendUrl,
    setBackendUrl: (url: string) => {
      if (url.startsWith('ftp:')) throw new errors.BackendUrlError('scheme', url)
      browser.backendUrl = url
    },
    setApiKey: (key: string | null) => (browser.apiKey = key || null),
    getHealth: vi.fn(async () => ({ status: 'ok' })),
    listLocalModels: browser.listLocalModels,
    downloadLocalModel: browser.downloadLocalModel,
    readStorage: () => null,
    writeStorage: () => undefined,
    toApiError: errors.toApiError,
  }
})

const { useHealthStore } = await import('./health')
const { useSettingsStore } = await import('./settings')

beforeEach(() => {
  browser.backendUrl = ''
  browser.apiKey = null
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

describe('settings → API key and backend host', () => {
  it('flags a stored key only when the backend moves to another origin', () => {
    browser.apiKey = 'secret'
    browser.backendUrl = 'http://gpu:8000'
    const settings = useSettingsStore()
    expect(settings.keyAtRisk('http://gpu:8000/')).toBe(false)
    expect(settings.keyAtRisk('http://other:8000')).toBe(true)
    expect(settings.keyAtRisk('')).toBe(true) // back to the page's own origin
    settings.clearApiKey()
    expect(settings.keyAtRisk('http://other:8000')).toBe(false) // nothing left to leak
  })

  it('clears the key together with the URL change when asked to', () => {
    browser.apiKey = 'secret'
    const settings = useSettingsStore()
    settings.setBackendUrl('http://other:8000')
    expect(settings.apiKey).toBe('secret') // kept unless asked
    settings.setBackendUrl('http://third:8000', { clearApiKey: true })
    expect(settings.apiKey).toBe('')
    expect(browser.apiKey).toBeNull()
    expect(settings.backendUrl).toBe('http://third:8000')
  })
})

describe('settings → local models', () => {
  it('loads the list, keeps the error, and refreshes after starting a download', async () => {
    const { ApiError } = await import('@/api/errors')
    const settings = useSettingsStore()
    browser.listLocalModels.mockRejectedValueOnce(new ApiError(401, null))
    await settings.loadLocalModels()
    expect(settings.localModelsError?.status).toBe(401)
    browser.listLocalModels.mockResolvedValue([{ id: 'qwen3-asr', status: 'downloading' }])
    browser.downloadLocalModel.mockResolvedValue({})
    await settings.downloadLocalModel('qwen3-asr', true)
    expect(browser.downloadLocalModel).toHaveBeenCalledWith('qwen3-asr', true)
    expect(settings.localModelsError).toBeNull()
    expect(settings.anyDownloading).toBe(true)
  })
})
