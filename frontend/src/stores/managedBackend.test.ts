/**
 * Managed backend ("desktop + backend" edition): status / edition detection (older core =
 * external), the first-run wizard's state machine (hardware → folders → install → done / failed,
 * cancel, skip + reopen, switch hardware, repair) and the 本机 profile following the sidecar the
 * setup starts. Runs against the in-memory fake platform.
 */
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setDesktopPlatformForTests } from '@/platform'
import {
  bundledManagedStatus,
  createFakeDesktop,
  FAKE_DEFAULT_DATA_DIR,
  FAKE_MANAGED_PORT,
  type FakeDesktop,
} from '@/platform/testing'

const storage = new Map<string, string>()

function stubWindow(desktop = true): void {
  vi.stubGlobal('window', {
    ...(desktop ? { __TAURI_INTERNALS__: {} } : {}),
    location: { href: 'http://tauri.localhost/' },
    localStorage: {
      getItem: (k: string) => storage.get(k) ?? null,
      setItem: (k: string, v: string) => storage.set(k, v),
      removeItem: (k: string) => storage.delete(k),
    },
  })
  vi.stubGlobal(
    'fetch',
    async () => new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } }),
  )
}

stubWindow() // the typed client is built at import time
const client = await import('@/api/client')
const { folderError, preferredHardware, LOG_LIMIT, useManagedBackendStore } = await import('./managedBackend')
const { useProfilesStore } = await import('./profiles')
const { useSidecarStore } = await import('./sidecar')

let fake: FakeDesktop

/** Let promise chains (status refresh after the final progress event, sidecar reload) settle. */
const settle = () => new Promise((r) => setTimeout(r, 0))

function bundled(patch: Partial<FakeDesktop['state']['managed']> = {}): void {
  fake.state.managed = bundledManagedStatus(patch)
}

beforeEach(() => {
  storage.clear()
  stubWindow()
  fake = createFakeDesktop()
  setDesktopPlatformForTests(fake)
  setActivePinia(createPinia())
})

afterEach(() => {
  setDesktopPlatformForTests(null)
  vi.unstubAllGlobals()
})

describe('helpers', () => {
  it('validates folders: blank = default, absolute only, no .. segments', () => {
    expect(folderError('')).toBeNull()
    expect(folderError('  ')).toBeNull()
    expect(folderError('D:\\ASR\\models')).toBeNull()
    expect(folderError('d:/asr')).toBeNull()
    expect(folderError('\\\\nas\\share\\models')).toBeNull()
    expect(folderError('/home/me/models')).toBeNull()
    expect(folderError('models')).toBe('relative')
    expect(folderError('D:models')).toBe('relative')
    expect(folderError('D:\\ASR\\..\\Windows')).toBe('parent')
    expect(folderError('D:\\AS|R')).toBe('chars')
  })

  it('prefers the current hardware when available, else the recommended one', () => {
    const list = fake.state.suggestions
    expect(preferredHardware(list, null)).toBe('rocm')
    expect(preferredHardware(list, 'cpu')).toBe('cpu')
    expect(preferredHardware(list, 'cuda')).toBe('rocm') // not available here
    expect(preferredHardware([{ ...list[1]! }], null)).toBeNull()
  })
})

describe('edition detection', () => {
  it('is inert in the web build', async () => {
    stubWindow(false)
    const store = useManagedBackendStore()
    await store.startup()
    expect(fake.calls).toEqual([])
    expect(store.bundled).toBe(false)
    expect(store.wizardOpen).toBe(false)
  })

  it('treats an older core without the commands as the plain build', async () => {
    fake.state.missing.add('managed_backend_status')
    const store = useManagedBackendStore()
    await store.startup()
    expect(store.loaded).toBe(true)
    expect(store.status.edition).toBe('external')
    expect(store.error).toMatch(/not found/)
    expect(store.wizardOpen).toBe(false)
    expect(fake.listenerCount()).toBe(0) // the managed listeners were dropped again
  })

  it('shows nothing in the external edition', async () => {
    const store = useManagedBackendStore()
    await store.startup()
    expect(store.bundled).toBe(false)
    expect(store.needsSetup).toBe(false)
    store.openWizard()
    expect(store.wizardOpen).toBe(false)
  })
})

describe('first-run wizard', () => {
  it('opens at startup, preselects the recommended hardware, installs, follows the sidecar', async () => {
    bundled()
    const store = useManagedBackendStore()
    const profiles = useProfilesStore()
    profiles.init()
    await store.startup()
    await settle()
    expect(store.wizardOpen).toBe(true)
    expect(store.step).toBe('hardware')
    expect(store.suggestions.map((s) => s.id)).toEqual(['cpu', 'cuda', 'rocm'])
    expect(store.choice.hardware).toBe('rocm')

    store.next()
    expect(store.step).toBe('folders')
    store.back()
    expect(store.step).toBe('hardware')
    store.next()
    store.choice.modelsDir = 'models'
    expect(store.foldersValid).toBe(false)
    store.choice.modelsDir = 'E:\\ASR\\models'
    expect(store.foldersValid).toBe(true)

    const epoch = profiles.epoch
    await store.install()
    expect(fake.state.lastInstall).toEqual({ hardware: 'rocm', dataDir: null, modelsDir: 'E:\\ASR\\models' })
    expect(store.step).toBe('install')
    expect(store.installing).toBe(true)

    fake.tickInstall()
    fake.tickInstall()
    expect(store.progress?.phase).toBe('python')
    expect(store.percent).toBe(10)
    expect(store.log).toEqual(['checking disk space', 'Installed Python 3.12.11'])

    fake.runInstall()
    await settle()
    expect(store.step).toBe('done')
    expect(store.installed).toBe(true)
    expect(store.status.hardware).toBe('rocm')
    expect(store.status.modelsDir).toBe('E:\\ASR\\models')
    expect(store.status.dataDir).toBe(FAKE_DEFAULT_DATA_DIR) // not chosen: the core's default
    // The core wrote + started the sidecar; the SPA picked up its config and the 本机 profile
    // (active, own URL empty) follows it.
    const sidecar = useSidecarStore()
    expect(sidecar.config?.port).toBe(FAKE_MANAGED_PORT)
    expect(sidecar.status.state).toBe('running')
    expect(profiles.isLocalActive).toBe(true)
    expect(profiles.activeUrl).toBe(`http://127.0.0.1:${FAKE_MANAGED_PORT}`)
    // The pages reload against the backend that just came up; Rust gets its URL.
    expect(profiles.epoch).toBe(epoch + 1)
    expect(fake.state.config.baseUrl).toBe(`http://127.0.0.1:${FAKE_MANAGED_PORT}`)

    store.closeWizard()
    expect(store.wizardOpen).toBe(false)
    expect(store.skipped).toBe(false)
  })

  it('cpu path: picks another available hardware, never an unavailable one', async () => {
    bundled()
    const store = useManagedBackendStore()
    await store.startup()
    await settle()
    store.choice.hardware = 'cpu'
    store.next()
    await store.install()
    fake.runInstall()
    await settle()
    expect(fake.state.lastInstall?.hardware).toBe('cpu')
    expect(store.status.hardware).toBe('cpu')
    expect(store.step).toBe('done')
  })

  it('fails, shows the error + log, retries with the same options', async () => {
    bundled()
    fake.state.installFailAt = 2
    const store = useManagedBackendStore()
    await store.startup()
    await settle()
    store.next()
    store.choice.dataDir = 'D:\\ASR\\data'
    await store.install()
    fake.runInstall()
    await settle()
    expect(store.step).toBe('failed')
    expect(store.status.state).toBe('failed')
    expect(store.status.error).toMatch(/network/)
    expect(store.log.at(-1)).toMatch(/^error: /)

    fake.state.installFailAt = null
    await store.retry()
    expect(store.step).toBe('install')
    expect(store.log).toEqual([]) // a fresh log per attempt
    expect(fake.state.lastInstall).toEqual({ hardware: 'rocm', dataDir: 'D:\\ASR\\data', modelsDir: null })
    fake.runInstall()
    await settle()
    expect(store.step).toBe('done')
  })

  it('failed: "choose other hardware" goes back to the first step', async () => {
    bundled({ state: 'failed', error: 'boom' })
    const store = useManagedBackendStore()
    await store.startup()
    expect(store.step).toBe('failed')
    store.startOver()
    await settle()
    expect(store.step).toBe('hardware')
  })

  it('cancels a running setup', async () => {
    bundled()
    const store = useManagedBackendStore()
    await store.startup()
    await settle()
    store.next()
    await store.install()
    fake.tickInstall()
    await store.cancel()
    await settle()
    expect(store.status.state).toBe('not_installed')
    expect(store.cancelled).toBe(true)
    expect(store.step).toBe('hardware')
    expect(store.wizardOpen).toBe(true)
  })

  it('reports a refused install (already installing) and stays on the step', async () => {
    bundled()
    const store = useManagedBackendStore()
    await store.startup()
    await settle()
    store.next()
    fake.state.failNext = 'already installing'
    await expect(store.install()).rejects.toThrow('already installing')
    expect(store.step).toBe('folders')
    expect(store.requesting).toBe(false)
  })

  it('"稍后" closes it for this session; the switcher / Settings reopen it', async () => {
    bundled()
    const store = useManagedBackendStore()
    await store.startup()
    store.closeWizard()
    expect(store.skipped).toBe(true)
    await store.startup()
    expect(store.wizardOpen).toBe(false)
    store.openWizard()
    expect(store.wizardOpen).toBe(true)
    expect(store.step).toBe('hardware')
  })

  it('does not open by itself while a remote profile is active', async () => {
    client.writeProfiles({
      version: 1,
      activeId: 'p-nas',
      profiles: [
        { id: client.LOCAL_PROFILE_ID, name: '', url: '', apiKey: null },
        { id: 'p-nas', name: 'NAS', url: 'http://nas.local:8000', apiKey: 'k' },
      ],
    })
    bundled()
    const store = useManagedBackendStore()
    await store.startup()
    expect(store.needsSetup).toBe(true)
    expect(store.wizardOpen).toBe(false)
  })

  it('resumes on the install step when a setup is already running', async () => {
    bundled({ state: 'installing' })
    const store = useManagedBackendStore()
    await store.startup()
    expect(store.wizardOpen).toBe(true)
    expect(store.step).toBe('install')
    fake.emitManagedProgress({ phase: 'packages', message: 'x', percent: 50, logLine: 'line' })
    expect(store.percent).toBe(50)
  })

  it('keeps the last LOG_LIMIT log lines', async () => {
    bundled({ state: 'installing' })
    const store = useManagedBackendStore()
    await store.init()
    for (let i = 0; i < LOG_LIMIT + 50; i++) {
      fake.emitManagedProgress({ phase: 'packages', message: 'x', percent: null, logLine: `l${i}` })
    }
    expect(store.log).toHaveLength(LOG_LIMIT)
    expect(store.log[0]).toBe('l50')
    expect(store.log.at(-1)).toBe(`l${LOG_LIMIT + 49}`)
  })
})

describe('installed backend (Settings card)', () => {
  const installed = {
    state: 'installed' as const,
    hardware: 'rocm' as const,
    backendVersion: '0.9.0',
    dataDir: 'D:\\ASR\\data',
    modelsDir: 'E:\\models',
    envSizeBytes: 5_000_000_000,
  }

  it('does not open the wizard at startup', async () => {
    bundled(installed)
    const store = useManagedBackendStore()
    await store.startup()
    expect(store.installed).toBe(true)
    expect(store.wizardOpen).toBe(false)
  })

  it('does not reload the pages over unsaved work or for a remote profile', async () => {
    bundled({ state: 'installing' })
    client.writeProfiles({
      version: 1,
      activeId: 'p-nas',
      profiles: [
        { id: client.LOCAL_PROFILE_ID, name: '', url: '', apiKey: null },
        { id: 'p-nas', name: 'NAS', url: 'http://nas.local:8000', apiKey: 'k' },
      ],
    })
    const profiles = useProfilesStore()
    profiles.init()
    const store = useManagedBackendStore()
    await store.init()
    const epoch = profiles.epoch
    fake.runInstall()
    await settle()
    expect(store.installed).toBe(true)
    expect(profiles.epoch).toBe(epoch)
    expect(profiles.activeId).toBe('p-nas')
  })

  it('switches hardware: current preselected, folders kept, re-runs the setup', async () => {
    bundled(installed)
    const store = useManagedBackendStore()
    await store.startup()
    store.switchHardware()
    await settle()
    expect(store.wizardOpen).toBe(true)
    expect(store.step).toBe('hardware')
    expect(store.choice.hardware).toBe('rocm')
    expect(store.choice.modelsDir).toBe('E:\\models')
    store.choice.hardware = 'cpu'
    store.next()
    await store.install()
    expect(fake.state.lastInstall).toEqual({ hardware: 'cpu', dataDir: 'D:\\ASR\\data', modelsDir: 'E:\\models' })
    fake.runInstall()
    await settle()
    expect(store.step).toBe('done')
    expect(store.status.hardware).toBe('cpu')
  })

  it('repairs with the installed hardware and folders', async () => {
    bundled(installed)
    const store = useManagedBackendStore()
    await store.startup()
    await store.repair()
    expect(fake.state.lastInstall).toEqual({ hardware: 'rocm', dataDir: 'D:\\ASR\\data', modelsDir: 'E:\\models' })
    expect(store.wizardOpen).toBe(true)
    expect(store.step).toBe('install')
  })

  it('falls back to CPU when detection fails', async () => {
    bundled()
    fake.state.missing.add('managed_backend_detect')
    const store = useManagedBackendStore()
    await store.startup()
    await settle()
    expect(store.detectError).toMatch(/not found/)
    expect(store.suggestions.map((s) => s.id)).toEqual(['cpu'])
    expect(store.choice.hardware).toBe('cpu')
  })
})

describe('re-install keeps the previous environment (D76)', () => {
  const installedCpu = {
    state: 'installed' as const,
    hardware: 'cpu' as const,
    backendVersion: '0.9.0',
    dataDir: 'D:\\ASR\\data',
    modelsDir: 'E:\\models',
    envSizeBytes: 1_000_000_000,
  }

  it('a cancelled switch returns to the hardware step, still installed on the old hardware', async () => {
    bundled(installedCpu)
    const store = useManagedBackendStore()
    await store.startup()
    store.switchHardware()
    await settle()
    store.choice.hardware = 'rocm'
    store.next()
    await store.install()
    fake.tickInstall()
    await store.cancel()
    await settle()
    expect(store.status).toMatchObject({ state: 'installed', hardware: 'cpu' })
    expect(store.cancelled).toBe(true)
    expect(store.step).toBe('hardware') // not "done"
    expect(store.installed).toBe(true)
  })

  it('a failed switch shows "failed" although the status stays installed', async () => {
    bundled(installedCpu)
    fake.state.installFailAt = 1
    const store = useManagedBackendStore()
    await store.startup()
    store.switchHardware()
    await settle()
    store.choice.hardware = 'rocm'
    store.next()
    await store.install()
    fake.runInstall()
    await settle()
    expect(store.status).toMatchObject({ state: 'installed', hardware: 'cpu' })
    expect(store.status.error).toMatch(/network/)
    expect(store.step).toBe('failed')
    expect(store.needsSetup).toBe(false) // 本机 keeps working
  })

  it('tolerates the failed progress event arriving after the status', async () => {
    bundled(installedCpu)
    const store = useManagedBackendStore()
    await store.startup()
    store.switchHardware()
    await settle()
    store.next()
    await store.install()
    // The core reports the kept environment first (no error field), then the failure.
    fake.emitManagedStatus({ ...fake.state.managed, state: 'installed', hardware: 'cpu', error: null })
    expect(store.step).toBe('done')
    fake.emitManagedProgress({ phase: 'failed', message: 'failed', percent: null, logLine: null })
    expect(store.step).toBe('failed')
  })
})

describe('folders + reveal', () => {
  it('picks a folder with the current value (or the default) as start; cancel keeps the field', async () => {
    bundled()
    const store = useManagedBackendStore()
    await store.init()
    await store.pickFolder('models', 'Choose')
    expect(fake.calls.at(-1)).toEqual([
      'pick_folder',
      { title: 'Choose', defaultPath: fake.state.managed.defaultModelsDir },
    ])
    expect(store.choice.modelsDir).toBe('D:\\Picked\\Folder')
    fake.state.pickedFolder = null
    store.choice.dataDir = 'D:\\Mine'
    await store.pickFolder('data', 'Choose')
    expect(fake.calls.at(-1)).toEqual(['pick_folder', { title: 'Choose', defaultPath: 'D:\\Mine' }])
    expect(store.choice.dataDir).toBe('D:\\Mine')
  })

  it('reveals folders and reports IPC errors', async () => {
    bundled()
    const store = useManagedBackendStore()
    await store.init()
    await store.reveal('backend')
    expect(fake.calls.at(-1)).toEqual(['managed_backend_reveal', { which: 'backend' }])
    fake.state.missing.add('managed_backend_reveal')
    await expect(store.reveal('data')).rejects.toThrow(/not found/)
  })
})
