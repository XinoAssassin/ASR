import { afterEach, describe, expect, it, vi } from 'vitest'

import { getDesktop, ipcErrorMessage, isDesktop, setDesktopPlatformForTests } from './index'

/** Minimal stand-in for what the Tauri 2 runtime injects into the WebView. */
function fakeInternals() {
  const callbacks = new Map<number, (data: unknown) => void>()
  let nextId = 1
  const invoke = vi.fn(async (cmd: string, args: Record<string, unknown>) => {
    if (cmd === 'plugin:event|listen') return 42
    if (cmd === 'plugin:event|unlisten') return null
    if (cmd === 'recordings_list') return []
    return { cmd, args }
  })
  return {
    callbacks,
    invoke,
    internals: {
      invoke,
      transformCallback: (cb: (data: unknown) => void) => {
        const id = nextId++
        callbacks.set(id, cb)
        return id
      },
    },
    eventInternals: { unregisterListener: vi.fn() },
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
  setDesktopPlatformForTests(null)
})

describe('isDesktop / getDesktop', () => {
  it('is false in the web build and refuses the desktop facade', async () => {
    vi.stubGlobal('window', {})
    expect(isDesktop()).toBe(false)
    await expect(getDesktop()).rejects.toThrow(/not available/)
  })

  it('detects the Tauri runtime', () => {
    vi.stubGlobal('window', { __TAURI_INTERNALS__: {} })
    expect(isDesktop()).toBe(true)
  })
})

describe('tauri facade (docs/desktop-ipc.md)', () => {
  it('maps every command to its IPC name and argument shape', async () => {
    const fake = fakeInternals()
    vi.stubGlobal('window', {
      __TAURI_INTERNALS__: fake.internals,
      __TAURI_EVENT_PLUGIN_INTERNALS__: fake.eventInternals,
    })
    const p = await getDesktop()
    const options = { system: true, mic: false, outputDevice: null, inputDevice: 'in-1', outDir: null }
    const config = {
      baseUrl: 'http://127.0.0.1:8000',
      apiKey: '***',
      targetDir: '/data/rec',
      autoUpload: true,
      jobOptions: null,
      provider: 'cloud' as const,
      termSetIds: ['ts1'],
      extraTerms: [{ term: 'PyTorch', weight: 3 }],
    }
    await p.recorder.devices()
    await p.recorder.start(options)
    await p.recorder.stop()
    await p.recorder.status()
    await p.backendConfig.get()
    await p.backendConfig.set(config)
    await p.recordings.list()
    await p.recordings.upload('r1')
    await p.recordings.reveal('r1')
    await p.recorder.processes()
    await p.recorder.setOptions(options)
    const sidecar = { enabled: true, command: 'asr.exe', args: ['serve'], cwd: null, port: 8000 }
    await p.sidecar.getConfig()
    await p.sidecar.setConfig(sidecar)
    await p.sidecar.status()
    await p.sidecar.start()
    await p.sidecar.stop()
    const install = { hardware: 'rocm' as const, dataDir: null, modelsDir: 'E:\\models' }
    await p.managedBackend.status()
    await p.managedBackend.detect()
    await p.managedBackend.install(install)
    await p.managedBackend.cancel()
    const sent = fake.invoke.mock.calls.map(([cmd, args]) => [cmd, args])
    expect(sent).toEqual([
      ['recorder_devices', {}],
      ['recorder_start', { options }],
      ['recorder_stop', {}],
      ['recorder_status', {}],
      ['backend_get_config', {}],
      ['backend_set_config', { config }],
      ['recordings_list', {}],
      ['recording_upload', { id: 'r1' }],
      ['recording_reveal', { id: 'r1' }],
      ['recorder_processes', {}],
      ['recorder_set_options', { options }],
      ['sidecar_get_config', {}],
      ['sidecar_set_config', { config: sidecar }],
      ['sidecar_status', {}],
      ['sidecar_start', {}],
      ['sidecar_stop', {}],
      ['managed_backend_status', {}],
      ['managed_backend_detect', {}],
      ['managed_backend_install', { options: install }],
      ['managed_backend_cancel', {}],
    ])
  })

  it('delivers event payloads and unlistens', async () => {
    const fake = fakeInternals()
    vi.stubGlobal('window', {
      __TAURI_INTERNALS__: fake.internals,
      __TAURI_EVENT_PLUGIN_INTERNALS__: fake.eventInternals,
    })
    const p = await getDesktop()
    const seen: unknown[] = []
    const off = await p.events.onRecorderStatus((s) => seen.push(s))
    const [cmd, args] = fake.invoke.mock.calls.at(-1)!
    expect(cmd).toBe('plugin:event|listen')
    expect(args).toMatchObject({ event: 'recorder://status', target: { kind: 'Any' } })
    const payload = { state: 'recording', elapsed: 1.5 }
    fake.callbacks.get(args.handler as number)!({ event: 'recorder://status', id: 42, payload })
    expect(seen).toEqual([payload])

    await p.events.onRecordingChanged(() => {})
    expect(fake.invoke.mock.calls.at(-1)![1]).toMatchObject({ event: 'recordings://changed' })
    await p.events.onSidecarStatus(() => {})
    expect(fake.invoke.mock.calls.at(-1)![1]).toMatchObject({ event: 'sidecar://status' })
    await p.events.onManagedProgress(() => {})
    expect(fake.invoke.mock.calls.at(-1)![1]).toMatchObject({ event: 'managed-backend://progress' })
    await p.events.onManagedStatus(() => {})
    expect(fake.invoke.mock.calls.at(-1)![1]).toMatchObject({ event: 'managed-backend://status' })

    off()
    await vi.waitFor(() =>
      expect(fake.eventInternals.unregisterListener).toHaveBeenCalledWith('recorder://status', 42),
    )
  })
})

describe('ipcErrorMessage', () => {
  it('handles Rust strings, Errors and objects', () => {
    expect(ipcErrorMessage('already recording')).toBe('already recording')
    expect(ipcErrorMessage(new Error('boom'))).toBe('boom')
    expect(ipcErrorMessage({ message: 'nope' })).toBe('nope')
    expect(ipcErrorMessage({ code: 1 })).toBe('{"code":1}')
  })
})
