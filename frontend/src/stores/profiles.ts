/**
 * Backend profiles (local / remote switch). The built-in "本机" profile (`LOCAL_PROFILE_ID`; on
 * desktop it follows the running sidecar) plus user-added remote ones, each with its own URL and
 * X-API-Key, persisted by `@/api` (localStorage, migrated from the single URL / key of older
 * versions). Requests always go to the active profile.
 *
 * Switching backend: pending transcript edits are flushed first; what still could not be saved,
 * and uploads in progress, need the caller's confirmation. Then every per-backend store is reset
 * (jobs, transcript, uploads, term sets, SiYuan, settings, hardware, health), the routed view is
 * re-mounted (`epoch`) and, on desktop, the connection is pushed to Rust (`backend_set_config`),
 * with the recording upload settings (target dir, job options, term sets) kept per profile.
 */
import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'

import {
  BackendUrlError,
  LOCAL_PROFILE_ID,
  newProfileId,
  normalizeBackendUrl,
  probeAuth,
  probeHealth,
  profileStorageKey,
  profileUrl,
  readProfiles,
  readStorage,
  rebuildApiClient,
  setLocalRuntimeUrl,
  writeProfiles,
  writeStorage,
  type BackendProfile,
  type HealthOut,
  type StoredProfiles,
} from '@/api'
import { ipcErrorMessage, isDesktop, type BackendConfig, type SidecarStatus } from '@/platform'

import { useHardwareStore } from './hardware'
import { useHealthStore } from './health'
import { useJobsStore } from './jobs'
import { useRecorderStore } from './recorder'
import { useSettingsStore } from './settings'
import { useSidecarStore } from './sidecar'
import { useSiyuanStore } from './siyuan'
import { useTermSetsStore } from './termSets'
import { useTranscriptStore } from './transcript'
import { useUploadsStore } from './uploads'

export { LOCAL_PROFILE_ID, type BackendProfile }

/** Why a switch needs the user's OK. */
export type SwitchConcern = 'unsavedEdits' | 'uploadRunning'

export interface ChangeOptions {
  /** Ask the user; resolve false to cancel. Without it, any concern cancels. */
  confirm?: (concern: SwitchConcern) => Promise<boolean>
}

export interface ProfileInput {
  name: string
  url: string
  apiKey: string | null
}

export interface ProfileTestResult {
  health: HealthOut
  /** null: the backend does not require a key. */
  keyAccepted: boolean | null
}

/** BackendConfig fields of the desktop recorder that belong to one backend. */
type RecordingSettings = Pick<
  BackendConfig,
  'targetDir' | 'autoUpload' | 'jobOptions' | 'provider' | 'termSetIds' | 'extraTerms'
>

const RECORDING_SETTINGS_KEY = 'asr.desktopRecording'

function readRecordingSettings(profileId: string): Partial<RecordingSettings> | null {
  try {
    const raw: unknown = JSON.parse(readStorage(profileStorageKey(RECORDING_SETTINGS_KEY, profileId)) ?? 'null')
    return raw && typeof raw === 'object' ? (raw as Partial<RecordingSettings>) : null
  } catch {
    return null
  }
}

function writeRecordingSettings(profileId: string, c: BackendConfig): void {
  const value: RecordingSettings = {
    targetDir: c.targetDir,
    autoUpload: c.autoUpload,
    jobOptions: c.jobOptions,
    provider: c.provider,
    termSetIds: c.termSetIds ?? [],
    extraTerms: c.extraTerms ?? [],
  }
  writeStorage(profileStorageKey(RECORDING_SETTINGS_KEY, profileId), JSON.stringify(value))
}

/** Origin of an effective URL ('' = this page). */
function originOf(url: string): string {
  const base = typeof window !== 'undefined' && window.location ? window.location.href : 'http://localhost/'
  try {
    return new URL(url || '/', base).origin
  } catch {
    return url
  }
}

export const useProfilesStore = defineStore('profiles', () => {
  const state = ref<StoredProfiles>(readProfiles())
  /** Mirrors the desktop sidecar URL (the built-in profile follows it). */
  const runtimeUrl = ref('')
  const switching = ref(false)
  /** Bumped on every backend change; App re-mounts the routed view on it. */
  const epoch = ref(0)
  /** Last failure pushing the connection to the desktop core (null = in sync / web). */
  const desktopError = ref<string | null>(null)

  const profiles = computed(() => state.value.profiles)
  const activeId = computed(() => state.value.activeId)
  const active = computed<BackendProfile>(
    () => profiles.value.find((p) => p.id === activeId.value) ?? profiles.value[0]!,
  )
  const isLocalActive = computed(() => activeId.value === LOCAL_PROFILE_ID)

  /** Effective URL of a profile ('' = same origin); reactive to the sidecar URL. */
  function urlOf(profile: BackendProfile): string {
    void runtimeUrl.value
    return profileUrl(profile)
  }

  const activeUrl = computed(() => urlOf(active.value))

  function find(id: string): BackendProfile | undefined {
    return profiles.value.find((p) => p.id === id)
  }

  function persist(next: StoredProfiles): void {
    writeProfiles(next) // also rebuilds `api`
    state.value = readProfiles()
    useSettingsStore().refreshConnection()
  }

  // -- pending work ------------------------------------------------------------------------------

  /** Flush transcript edits, then report what leaving this backend would lose. */
  async function pendingWork(): Promise<SwitchConcern[]> {
    const transcript = useTranscriptStore()
    if (transcript.hasUnsavedChanges) {
      try {
        await transcript.flush()
      } catch {
        // still unsaved: reported below
      }
    }
    const concerns: SwitchConcern[] = []
    if (transcript.hasUnsavedChanges) concerns.push('unsavedEdits')
    const uploads = useUploadsStore()
    if (uploads.uploading || uploads.submitting) concerns.push('uploadRunning')
    return concerns
  }

  async function confirmed(options: ChangeOptions): Promise<boolean> {
    for (const concern of await pendingWork()) {
      if (!options.confirm || !(await options.confirm(concern))) return false
    }
    return true
  }

  /** Forget everything the previous backend said and start over against the active one. */
  function resetBackendState(): void {
    rebuildApiClient()
    useTranscriptStore().discard()
    useJobsStore().reset()
    useUploadsStore().resetForBackend()
    useTermSetsStore().reset()
    useSiyuanStore().reset()
    const settings = useSettingsStore()
    settings.resetForBackend()
    const hardware = useHardwareStore()
    hardware.reset()
    if (isDesktop()) useRecorderStore().forgetBackendState()
    const health = useHealthStore()
    health.reset()
    void health.check()
    void settings.load()
    void hardware.load()
    epoch.value++
  }

  /**
   * Desktop: hand Rust the active connection. Recording upload settings are per backend: the
   * previous profile's are saved, the new one's restored (else its term sets, which belong to
   * the previous backend, are dropped). Errors land in `desktopError`.
   */
  async function pushDesktop(fromId: string | null): Promise<void> {
    if (!isDesktop()) return
    const recorder = useRecorderStore()
    try {
      await recorder.init()
      if (fromId && fromId !== activeId.value) {
        if (recorder.config) writeRecordingSettings(fromId, recorder.config)
        await recorder.saveConfig(readRecordingSettings(activeId.value) ?? { termSetIds: [] })
      } else {
        await recorder.syncBackend()
      }
      desktopError.value = null
    } catch (e) {
      desktopError.value = ipcErrorMessage(e)
    }
  }

  // -- switching ---------------------------------------------------------------------------------

  /**
   * Make `id` the active backend. Resolves false when nothing changed (same profile, unknown id,
   * a switch already running, or the user declined a concern).
   */
  async function switchTo(id: string, options: ChangeOptions = {}): Promise<boolean> {
    if (id === activeId.value || !find(id) || switching.value) return false
    switching.value = true
    try {
      if (!(await confirmed(options))) return false
      const fromId = activeId.value
      persist({ ...readProfiles(), activeId: id })
      resetBackendState()
      await pushDesktop(fromId)
      return true
    } finally {
      switching.value = false
    }
  }

  // -- profile management ------------------------------------------------------------------------

  function cleanName(name: string, url: string): string {
    const trimmed = name.trim()
    if (trimmed) return trimmed
    try {
      return new URL(url).host
    } catch {
      return url
    }
  }

  /** Add a remote profile (not activated). Throws BackendUrlError for an unusable URL. */
  function add(input: ProfileInput): BackendProfile {
    const url = normalizeBackendUrl(input.url)
    if (!url) throw new BackendUrlError('invalid', input.url.trim())
    const profile: BackendProfile = {
      id: newProfileId(),
      name: cleanName(input.name, url),
      url,
      apiKey: input.apiKey?.trim() || null,
    }
    const current = readProfiles()
    persist({ ...current, profiles: [...current.profiles, profile] })
    return find(profile.id) ?? profile
  }

  /**
   * Whether saving `url` for profile `id` would send its stored key to another origin: the key
   * was meant for the current backend, so the UI offers to clear it. Throws BackendUrlError.
   */
  function keyAtRisk(id: string, url: string): boolean {
    const profile = find(id)
    if (!profile?.apiKey) return false
    const next = normalizeBackendUrl(url)
    const nextUrl = next || (id === LOCAL_PROFILE_ID ? profileUrl({ ...profile, url: '' }) : '')
    return originOf(nextUrl) !== originOf(urlOf(profile))
  }

  /** Whether giving active profile `id` this URL points it at another origin. Throws BackendUrlError. */
  function changesBackend(id: string, url: string): boolean {
    const profile = find(id)
    if (!profile || id !== activeId.value) return false
    return originOf(profileUrl({ ...profile, url: normalizeBackendUrl(url) })) !== originOf(urlOf(profile))
  }

  /**
   * Edit a profile. `url` empty is allowed for the built-in profile only (= its default). When
   * the active profile now points at another origin this is a backend change (same concerns and
   * resets as a switch). Throws BackendUrlError (nothing saved). Resolves false when cancelled.
   */
  async function update(
    id: string,
    patch: Partial<ProfileInput>,
    options: ChangeOptions & { clearApiKey?: boolean } = {},
  ): Promise<boolean> {
    const profile = find(id)
    if (!profile) return false
    const next: BackendProfile = { ...profile }
    if (patch.url !== undefined) {
      next.url = normalizeBackendUrl(patch.url)
      if (!next.url && id !== LOCAL_PROFILE_ID) throw new BackendUrlError('invalid', patch.url.trim())
    }
    if (patch.name !== undefined && id !== LOCAL_PROFILE_ID) next.name = cleanName(patch.name, next.url)
    if (patch.apiKey !== undefined) next.apiKey = patch.apiKey?.trim() || null
    if (options.clearApiKey) next.apiKey = null

    const isActive = id === activeId.value
    const backendChanged = isActive && originOf(profileUrl(next)) !== originOf(urlOf(profile))
    const urlChanged = isActive && profileUrl(next) !== urlOf(profile)
    const keyChanged = isActive && next.apiKey !== profile.apiKey
    if (backendChanged && !(await confirmed(options))) return false

    const current = readProfiles()
    persist({ ...current, profiles: current.profiles.map((p) => (p.id === id ? next : p)) })
    if (backendChanged) {
      resetBackendState()
    } else if (urlChanged || keyChanged) {
      // Same backend, other path / key: a 401 from before says nothing about the new key.
      const health = useHealthStore()
      health.clearUnauthorized()
      void health.check()
      if (keyChanged) void useSettingsStore().load()
    }
    if (urlChanged || keyChanged) await pushDesktop(null)
    return true
  }

  /** Remove a remote profile. The active one has to be switched away from first. */
  function remove(id: string): void {
    if (id === LOCAL_PROFILE_ID) throw new Error('the built-in profile cannot be removed')
    if (id === activeId.value) throw new Error('switch to another backend first')
    const current = readProfiles()
    persist({ ...current, profiles: current.profiles.filter((p) => p.id !== id) })
  }

  /**
   * Test a (possibly unsaved) URL + key: health, and — when the backend requires a key — whether
   * this key is accepted. Throws BackendUrlError / ApiError. `url` '' = the built-in default.
   */
  async function test(url: string, apiKey: string | null): Promise<ProfileTestResult> {
    const target = normalizeBackendUrl(url) || profileUrl({ id: LOCAL_PROFILE_ID, name: '', url: '', apiKey: null })
    const health = await probeHealth(target)
    const keyAccepted = health.api_key_required ? await probeAuth(target, apiKey?.trim() || null) : null
    return { health, keyAccepted }
  }

  // -- desktop sidecar ---------------------------------------------------------------------------

  /** The running sidecar's URL changed: the built-in profile follows it (same backend). */
  function onSidecarStatus(status: SidecarStatus): void {
    const url = status.state === 'running' ? status.url : null
    const before = activeUrl.value
    setLocalRuntimeUrl(url)
    runtimeUrl.value = url ?? ''
    if (!isLocalActive.value || activeUrl.value === before) return
    rebuildApiClient()
    const health = useHealthStore()
    health.clearUnauthorized()
    void health.check()
    void pushDesktop(null)
  }

  let initialized = false

  /** Desktop: follow the sidecar. Idempotent; a no-op in the web build. */
  function init(): void {
    if (initialized || !isDesktop()) return
    initialized = true
    const sidecar = useSidecarStore()
    watch(() => sidecar.status, onSidecarStatus, { immediate: true, deep: true })
    void sidecar.init()
  }

  /**
   * Point the built-in profile at the running sidecar (its own URL cleared) and make it active.
   * Resolves false when the user cancelled the switch.
   */
  async function useSidecar(options: ChangeOptions = {}): Promise<boolean> {
    onSidecarStatus(useSidecarStore().status) // follow it even before init()
    if (find(LOCAL_PROFILE_ID)?.url) {
      if (!(await update(LOCAL_PROFILE_ID, { url: '' }, options))) return false
    }
    if (isLocalActive.value) {
      await pushDesktop(null)
      return true
    }
    return switchTo(LOCAL_PROFILE_ID, options)
  }

  return {
    profiles,
    activeId,
    active,
    activeUrl,
    isLocalActive,
    switching,
    epoch,
    desktopError,
    urlOf,
    find,
    pendingWork,
    switchTo,
    add,
    update,
    remove,
    keyAtRisk,
    changesBackend,
    test,
    init,
    useSidecar,
  }
})
