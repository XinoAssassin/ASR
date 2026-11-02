/**
 * Desktop implementation of the platform facade (Tauri 2 IPC, `docs/desktop-ipc.md`).
 * Loaded only through the dynamic import in `./index.ts` when `isDesktop()`, so the web bundle
 * never pulls `@tauri-apps/api` into its entry chunk. Do not import this module statically.
 */
import { invoke } from '@tauri-apps/api/core'
import { listen } from '@tauri-apps/api/event'

import type {
  BackendConfig,
  DesktopPlatform,
  DeviceList,
  HardwareSuggestion,
  ManagedBackendStatus,
  ManagedFolder,
  ManagedInstallOptions,
  ManagedProgress,
  PickFolderOptions,
  ProxySettings,
  ProxyState,
  ProxyTestResult,
  Recording,
  AudioProcess,
  RecorderStatus,
  SidecarConfig,
  SidecarStatus,
  StartOptions,
} from './types'

export const EVENTS = {
  recorderStatus: 'recorder://status',
  recordingsChanged: 'recordings://changed',
  sidecarStatus: 'sidecar://status',
  managedProgress: 'managed-backend://progress',
  managedStatus: 'managed-backend://status',
} as const

export function createTauriPlatform(): DesktopPlatform {
  return {
    recorder: {
      devices: () => invoke<DeviceList>('recorder_devices'),
      start: (options: StartOptions) => invoke<RecorderStatus>('recorder_start', { options }),
      stop: () => invoke<Recording>('recorder_stop'),
      status: () => invoke<RecorderStatus>('recorder_status'),
      processes: () => invoke<AudioProcess[]>('recorder_processes'),
      setOptions: (options: StartOptions) => invoke<void>('recorder_set_options', { options }),
    },
    sidecar: {
      getConfig: () => invoke<SidecarConfig>('sidecar_get_config'),
      setConfig: (config: SidecarConfig) => invoke<SidecarConfig>('sidecar_set_config', { config }),
      status: () => invoke<SidecarStatus>('sidecar_status'),
      start: () => invoke<SidecarStatus>('sidecar_start'),
      stop: () => invoke<SidecarStatus>('sidecar_stop'),
    },
    managedBackend: {
      status: () => invoke<ManagedBackendStatus>('managed_backend_status'),
      detect: () => invoke<HardwareSuggestion[]>('managed_backend_detect'),
      install: (options: ManagedInstallOptions) =>
        invoke<ManagedBackendStatus>('managed_backend_install', { options }),
      cancel: () => invoke<ManagedBackendStatus>('managed_backend_cancel'),
      reveal: (which: ManagedFolder) => invoke<void>('managed_backend_reveal', { which }),
    },
    dialog: {
      pickFolder: (options: PickFolderOptions = {}) =>
        invoke<string | null>('pick_folder', { title: options.title, defaultPath: options.defaultPath ?? null }),
    },
    network: {
      getProxy: () => invoke<ProxyState>('network_get_proxy'),
      setProxy: (settings: ProxySettings) => invoke<ProxyState>('network_set_proxy', { settings }),
      testProxy: (settings?: ProxySettings) =>
        invoke<ProxyTestResult>('network_test_proxy', settings ? { settings } : {}),
    },
    backendConfig: {
      get: () => invoke<BackendConfig>('backend_get_config'),
      set: (config: BackendConfig) => invoke<BackendConfig>('backend_set_config', { config }),
    },
    recordings: {
      list: () => invoke<Recording[]>('recordings_list'),
      upload: (id: string) => invoke<Recording>('recording_upload', { id }),
      reveal: (id: string) => invoke<void>('recording_reveal', { id }),
    },
    events: {
      onRecorderStatus: (handler) =>
        listen<RecorderStatus>(EVENTS.recorderStatus, (e) => handler(e.payload)),
      onRecordingChanged: (handler) =>
        listen<Recording>(EVENTS.recordingsChanged, (e) => handler(e.payload)),
      onSidecarStatus: (handler) =>
        listen<SidecarStatus>(EVENTS.sidecarStatus, (e) => handler(e.payload)),
      onManagedProgress: (handler) =>
        listen<ManagedProgress>(EVENTS.managedProgress, (e) => handler(e.payload)),
      onManagedStatus: (handler) =>
        listen<ManagedBackendStatus>(EVENTS.managedStatus, (e) => handler(e.payload)),
    },
  }
}
