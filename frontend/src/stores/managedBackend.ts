/**
 * "Desktop + backend" edition (docs/desktop-ipc.md): the backend environment the desktop app
 * creates on first run and then runs as its sidecar. Status (`managed-backend://status`), hardware
 * detection, install / cancel with live progress (`managed-backend://progress`, last log lines),
 * and the first-run wizard's state machine:
 *
 *   hardware → folders → install ─┬─ done
 *       ↑                         └─ failed ─(retry)→ install
 *       └──── cancel / choose other hardware ─┘
 *
 * The wizard opens by itself once at startup when the bundled backend is not installed and the
 * built-in "本机" profile is active; "稍后" closes it for this session, and the backend switcher /
 * Settings reopen it. An older core without these commands counts as the plain build
 * (`edition: 'external'`): nothing of this is shown.
 */
import { defineStore } from 'pinia'
import { computed, reactive, ref, shallowRef, watch } from 'vue'

import {
  getDesktop,
  ipcErrorMessage,
  isDesktop,
  type HardwareSuggestion,
  type ManagedBackendStatus,
  type ManagedHardware,
  type ManagedInstallOptions,
  type ManagedProgress,
  type Unlisten,
} from '@/platform'

import { useProfilesStore } from './profiles'
import { useSidecarStore } from './sidecar'

export type WizardStep = 'hardware' | 'folders' | 'install' | 'done' | 'failed'

/** Log lines kept for the install log view. */
export const LOG_LIMIT = 200

/** Approximate download per hardware (docs/desktop-ipc.md), in GB. */
export const DOWNLOAD_GB: Record<ManagedHardware, number> = { cpu: 1, cuda: 2.5, rocm: 5 }

export function externalStatus(): ManagedBackendStatus {
  return {
    edition: 'external',
    state: 'not_installed',
    hardware: null,
    backendVersion: null,
    dataDir: null,
    modelsDir: null,
    envSizeBytes: null,
    error: null,
  }
}

/** Only CPU is certain when detection failed. */
export function fallbackSuggestions(): HardwareSuggestion[] {
  return [{ id: 'cpu', available: true, recommended: true, label: 'CPU', detail: null }]
}

/**
 * Validate a folder typed by the user: blank = default; else an absolute Windows (`C:\…`, UNC
 * `\\server\share`) or POSIX path without `..` segments or control characters. Returns an error
 * key (`managed.folders.errors.*`) or null.
 */
export function folderError(path: string): 'relative' | 'parent' | 'chars' | null {
  const p = path.trim()
  if (!p) return null
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f<>"|?*]/.test(p.replace(/^[A-Za-z]:/, ''))) return 'chars'
  if (!/^[A-Za-z]:[\\/]/.test(p) && !/^\\\\[^\\]+\\[^\\]+/.test(p) && !p.startsWith('/')) return 'relative'
  if (p.split(/[\\/]+/).includes('..')) return 'parent'
  return null
}

/** The preselected hardware: the current one when it is available, else the recommended one. */
export function preferredHardware(
  suggestions: readonly HardwareSuggestion[],
  current: ManagedHardware | null,
): ManagedHardware | null {
  const ok = suggestions.filter((s) => s.available)
  if (current && ok.some((s) => s.id === current)) return current
  return (ok.find((s) => s.recommended) ?? ok[0])?.id ?? null
}

export const useManagedBackendStore = defineStore('managedBackend', () => {
  const available = isDesktop()
  const status = ref<ManagedBackendStatus>(externalStatus())
  const loaded = ref(false)
  /** Load error (an older core: treated as the plain build). */
  const error = ref<string | null>(null)

  const suggestions = ref<HardwareSuggestion[]>([])
  const detecting = ref(false)
  const detectError = ref<string | null>(null)

  const progress = ref<ManagedProgress | null>(null)
  const log = ref<string[]>([])
  /** The install / cancel IPC call is running. */
  const requesting = ref(false)

  // -- wizard --------------------------------------------------------------------------------
  const wizardOpen = ref(false)
  const step = ref<WizardStep>('hardware')
  const choice = reactive({ hardware: null as ManagedHardware | null, dataDir: '', modelsDir: '' })
  /** "稍后" was chosen this session: no automatic opening again. */
  const skipped = ref(false)
  /** The last setup was cancelled by the user (shown on the hardware step). */
  const cancelled = ref(false)
  /** Options of the last install request, for "retry". */
  const lastOptions = shallowRef<ManagedInstallOptions | null>(null)

  const bundled = computed(() => status.value.edition === 'bundled')
  const installed = computed(() => bundled.value && status.value.state === 'installed')
  const installing = computed(() => bundled.value && status.value.state === 'installing')
  /** Bundled but not usable yet (not installed, installing or failed). */
  const needsSetup = computed(() => bundled.value && status.value.state !== 'installed')
  const percent = computed(() => progress.value?.percent ?? null)

  let unlisten: Unlisten[] = []
  let initPromise: Promise<void> | null = null
  /** A setup finished: reload the pages once the sidecar it started is running. */
  let reloadPending = false

  function reloadWhenRunning(): void {
    const sc = useSidecarStore().status
    if (!reloadPending || sc.state !== 'running' || !sc.url) return
    reloadPending = false
    void useProfilesStore().reloadLocal()
  }

  function applyStatus(s: ManagedBackendStatus): void {
    const prev = status.value.state
    status.value = s
    if (s.state === 'installing') {
      step.value = 'install'
    } else if (step.value === 'install') {
      if (s.state === 'installed') step.value = 'done'
      else if (s.state === 'failed') step.value = 'failed'
      else step.value = 'hardware' // cancelled: the partial environment is gone
    }
    if (s.state === 'installed' && prev === 'installing') {
      // The core wrote + started the sidecar: pick up its config (the status follows by event),
      // then reload what the pages showed while there was no backend.
      reloadPending = true
      void useSidecarStore().reload().then(reloadWhenRunning)
    }
  }

  function applyProgress(p: ManagedProgress): void {
    progress.value = p
    if (p.logLine) {
      log.value.push(p.logLine)
      if (log.value.length > LOG_LIMIT) log.value.splice(0, log.value.length - LOG_LIMIT)
    }
    // The final event: make sure the status is current even if its event got lost.
    if (p.phase === 'done' || p.phase === 'failed') void refresh()
  }

  function init(): Promise<void> {
    if (!available) return Promise.resolve()
    initPromise ??= (async () => {
      const platform = await getDesktop()
      watch(() => useSidecarStore().status, reloadWhenRunning, { deep: true })
      try {
        unlisten = await Promise.all([
          platform.events.onManagedStatus(applyStatus),
          platform.events.onManagedProgress(applyProgress),
        ])
        applyStatus(await platform.managedBackend.status())
        error.value = null
      } catch (e) {
        // An older core without the managed commands: the plain build.
        unlisten.forEach((u) => u())
        unlisten = []
        status.value = externalStatus()
        error.value = ipcErrorMessage(e)
      }
      loaded.value = true
    })().catch((e: unknown) => {
      initPromise = null
      error.value = ipcErrorMessage(e)
      loaded.value = true
    })
    return initPromise
  }

  function dispose(): void {
    unlisten.forEach((u) => u())
    unlisten = []
    initPromise = null
  }

  async function refresh(): Promise<void> {
    if (!available || !bundled.value) return
    try {
      applyStatus(await (await getDesktop()).managedBackend.status())
    } catch {
      // keep what the events said
    }
  }

  async function detect(): Promise<void> {
    detecting.value = true
    try {
      const list = await (await getDesktop()).managedBackend.detect()
      suggestions.value = list.length ? list : fallbackSuggestions()
      detectError.value = null
    } catch (e) {
      suggestions.value = fallbackSuggestions()
      detectError.value = ipcErrorMessage(e)
    } finally {
      detecting.value = false
    }
    const selected = suggestions.value.find((s) => s.id === choice.hardware)
    if (!selected?.available) choice.hardware = preferredHardware(suggestions.value, status.value.hardware)
  }

  /** Open the wizard on the step that fits the current state. */
  function openWizard(): void {
    if (!bundled.value) return
    wizardOpen.value = true
    switch (status.value.state) {
      case 'installing':
        step.value = 'install'
        return
      case 'failed':
        step.value = 'failed'
        return
      default:
        startOver()
    }
  }

  /** "切换硬件": the wizard on the hardware step (not while a setup runs). */
  function switchHardware(): void {
    if (!bundled.value) return
    wizardOpen.value = true
    if (status.value.state === 'installing') step.value = 'install'
    else startOver()
  }

  /** Back to the hardware step with the folders of the current install (if any). */
  function startOver(): void {
    step.value = 'hardware'
    choice.dataDir = status.value.dataDir ?? ''
    choice.modelsDir = status.value.modelsDir ?? ''
    choice.hardware = suggestions.value.length
      ? preferredHardware(suggestions.value, status.value.hardware)
      : status.value.hardware
    if (!detecting.value) void detect()
  }

  /** Open the wizard by itself once at startup (bundled, not installed, 本机 active). */
  async function startup(): Promise<void> {
    await init()
    if (needsSetup.value && !skipped.value && useProfilesStore().isLocalActive) openWizard()
  }

  /** "稍后" / hide: close; a running setup continues in the background. */
  function closeWizard(): void {
    wizardOpen.value = false
    if (!installed.value) skipped.value = true
  }

  function next(): void {
    if (step.value === 'hardware' && choice.hardware) step.value = 'folders'
  }

  function back(): void {
    if (step.value === 'folders') step.value = 'hardware'
  }

  const foldersValid = computed(() => !folderError(choice.dataDir) && !folderError(choice.modelsDir))

  /** Start the setup with `options` (default: the wizard's choice). Throws the IPC error. */
  async function install(options?: ManagedInstallOptions): Promise<void> {
    // A plain object (not a reactive proxy): it goes over IPC and is kept for "retry".
    const opts: ManagedInstallOptions = options
      ? { ...options }
      : {
          hardware: choice.hardware ?? 'cpu',
          dataDir: choice.dataDir.trim() || null,
          modelsDir: choice.modelsDir.trim() || null,
        }
    requesting.value = true
    progress.value = null
    log.value = []
    cancelled.value = false
    lastOptions.value = opts
    try {
      applyStatus(await (await getDesktop()).managedBackend.install(opts))
      step.value = status.value.state === 'installing' ? 'install' : step.value
    } catch (e) {
      throw new Error(ipcErrorMessage(e))
    } finally {
      requesting.value = false
    }
  }

  /** Same options as last time (or the current install's). */
  function retry(): Promise<void> {
    return install(lastOptions.value ?? currentOptions())
  }

  /** Re-run the setup for the installed hardware and folders ("修复 / 重新安装"). */
  async function repair(): Promise<void> {
    await install(currentOptions())
    wizardOpen.value = true
  }

  function currentOptions(): ManagedInstallOptions {
    return {
      hardware: status.value.hardware ?? choice.hardware ?? 'cpu',
      dataDir: status.value.dataDir,
      modelsDir: status.value.modelsDir,
    }
  }

  /** Stop a running setup. Throws the IPC error. */
  async function cancel(): Promise<void> {
    requesting.value = true
    try {
      applyStatus(await (await getDesktop()).managedBackend.cancel())
      cancelled.value = status.value.state !== 'installing'
      if (cancelled.value) startOver()
    } catch (e) {
      throw new Error(ipcErrorMessage(e))
    } finally {
      requesting.value = false
    }
  }

  return {
    available,
    status,
    loaded,
    error,
    suggestions,
    detecting,
    detectError,
    progress,
    percent,
    log,
    requesting,
    wizardOpen,
    step,
    choice,
    skipped,
    cancelled,
    lastOptions,
    bundled,
    installed,
    installing,
    needsSetup,
    foldersValid,
    init,
    dispose,
    refresh,
    detect,
    startup,
    openWizard,
    switchHardware,
    startOver,
    closeWizard,
    next,
    back,
    install,
    retry,
    repair,
    cancel,
  }
})
