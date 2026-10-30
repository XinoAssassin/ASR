/**
 * The only module that talks to the backend. Every API call goes through `api` (typed from the
 * backend OpenAPI document, see `pnpm gen:api`) or, for non-JSON transports (SSE via
 * @microsoft/fetch-event-source, audio blobs), through `apiUrl()` + `authHeaders()`.
 *
 * Backends are *profiles* (localStorage `asr.backendProfiles`): the built-in "本机" profile
 * (`LOCAL_PROFILE_ID`) plus user-added remote ones, each with its own URL and X-API-Key. Requests
 * go to the active profile. URL of the built-in profile: its own URL when set, else (desktop) the
 * running sidecar's URL, else window.__ASR_CONFIG__.backendUrl (config.js), else "" (same origin;
 * the Vite dev proxy forwards /api to :8000) — in the desktop app http://127.0.0.1:8000 instead,
 * the WebView origin (tauri.localhost) has no API. Remote profiles always have a URL.
 * The X-API-Key header is added from the active profile on every request; never put it in a URL
 * (D6). The single URL / key of older versions is migrated into a profile on first read.
 */
import createClient, { type Client, type Middleware } from 'openapi-fetch'

import { DESKTOP_DEFAULT_BACKEND, isDesktop } from '@/platform'

import { BackendUrlError } from './errors'
import type { components, paths } from './schema'

export type Schemas = components['schemas']

export const STORAGE_KEYS = {
  /** Legacy (single backend), migrated into `profiles` on first read. */
  backendUrl: 'asr.backendUrl',
  /** Legacy (single backend), migrated into `profiles` on first read. */
  apiKey: 'asr.apiKey',
  profiles: 'asr.backendProfiles',
} as const

/** Id of the built-in "本机" (this computer) profile; it cannot be removed. */
export const LOCAL_PROFILE_ID = 'local'

export interface BackendProfile {
  /** `LOCAL_PROFILE_ID` or a generated `p-…` id. */
  id: string
  /** Display name; empty for the built-in profile (the UI shows "本机"). */
  name: string
  /** Normalised http(s) URL; empty only for the built-in profile (= the default backend). */
  url: string
  /** X-API-Key for this backend (localStorage, like before; never in a URL). */
  apiKey: string | null
}

export interface StoredProfiles {
  version: 1
  activeId: string
  profiles: BackendProfile[]
}

/**
 * Browser-local keys whose value belongs to one backend (server paths): stored per profile as
 * `<key>@<profileId>`. Migration moves the legacy unsuffixed value to the migrated profile.
 */
export const PER_BACKEND_KEYS = ['asr.lastTargetDir', 'asr.recentDirs'] as const

export function profileStorageKey(key: string, profileId: string): string {
  return `${key}@${profileId}`
}

export const API_KEY_HEADER = 'X-API-Key'

function readStorage(key: string): string | null {
  try {
    return window.localStorage.getItem(key)
  } catch {
    return null
  }
}

function writeStorage(key: string, value: string | null): void {
  try {
    if (value) window.localStorage.setItem(key, value)
    else window.localStorage.removeItem(key)
  } catch {
    // Storage unavailable (private mode); the setting lasts for this page only.
  }
}

function normalizeBaseUrl(url: string | null | undefined): string {
  return (url ?? '').trim().replace(/\/+$/, '')
}

/** `scheme:` not followed by a digit: `ftp://x`, `javascript:…`, but not `gpu:8000`. */
const HAS_SCHEME = /^[a-z][a-z0-9+.-]*:(?!\d)/i

/**
 * Validate and normalise a backend URL typed by the user: trimmed, `http://` added when there is
 * no scheme (`gpu:8000` → `http://gpu:8000`), no trailing slash. Empty stays empty (= no
 * override). Throws BackendUrlError for anything that is not an http(s) URL.
 */
export function normalizeBackendUrl(input: string | null | undefined): string {
  const raw = normalizeBaseUrl(input)
  if (!raw) return ''
  const withScheme = HAS_SCHEME.test(raw) ? raw : `http://${raw}`
  let parsed: URL
  try {
    parsed = new URL(withScheme)
  } catch {
    throw new BackendUrlError('invalid', raw)
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new BackendUrlError('scheme', raw)
  }
  if (!parsed.hostname) throw new BackendUrlError('invalid', raw)
  return normalizeBaseUrl(withScheme)
}

/** `normalizeBackendUrl` for stored / configured values: an unusable one counts as unset. */
function safeBackendUrl(input: string | null | undefined): string {
  try {
    return normalizeBackendUrl(input)
  } catch {
    return ''
  }
}

export function getBackendUrl(): string {
  return profileUrl(activeProfile())
}

export function getApiKey(): string | null {
  return activeProfile().apiKey?.trim() || null
}

/**
 * The backend URL of the built-in profile when its own URL is empty: the running desktop sidecar,
 * else config.js, else same origin on the web / the local backend in the desktop app.
 */
export function getDefaultBackendUrl(): string {
  if (isDesktop() && localRuntimeUrl) return localRuntimeUrl
  const configured = safeBackendUrl(typeof window === 'undefined' ? '' : window.__ASR_CONFIG__?.backendUrl)
  if (configured) return configured
  return isDesktop() ? DESKTOP_DEFAULT_BACKEND : ''
}

/** Effective URL of a profile ('' = same origin). */
export function profileUrl(profile: BackendProfile): string {
  return profile.url || (profile.id === LOCAL_PROFILE_ID ? getDefaultBackendUrl() : '')
}

// --- profiles ------------------------------------------------------------------------------------

/** URL of the desktop sidecar while it runs (not persisted; set by the sidecar store). */
let localRuntimeUrl = ''

/** Desktop: the running sidecar's URL (null when stopped); used by the built-in profile. */
export function setLocalRuntimeUrl(url: string | null): void {
  localRuntimeUrl = safeBackendUrl(url)
}

function localProfile(): BackendProfile {
  return { id: LOCAL_PROFILE_ID, name: '', url: '', apiKey: null }
}

const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '[::1]', '::1'])

/** `http://127.0.0.1:8000`, `http://localhost:8000`, … : a backend on this computer. */
export function isLoopbackUrl(url: string): boolean {
  try {
    return LOOPBACK_HOSTS.has(new URL(url).hostname.toLowerCase())
  } catch {
    return false
  }
}

function hostLabel(url: string): string {
  try {
    return new URL(url).host
  } catch {
    return url
  }
}

/**
 * First run of the profile version: the single stored URL / key becomes a profile. A loopback
 * URL (or none) goes to the built-in profile (empty URL when it is the default anyway, so it
 * follows the desktop sidecar); any other URL becomes an active remote profile named after its
 * host. Per-backend keys (upload directories) move with it.
 */
function migrateLegacy(): StoredProfiles {
  const url = safeBackendUrl(readStorage(STORAGE_KEYS.backendUrl))
  const key = readStorage(STORAGE_KEYS.apiKey)?.trim() || null
  const local = localProfile()
  const state: StoredProfiles = { version: 1, activeId: LOCAL_PROFILE_ID, profiles: [local] }
  if (!url || isLoopbackUrl(url)) {
    local.url = url && url !== DESKTOP_DEFAULT_BACKEND ? url : ''
    local.apiKey = key
  } else {
    const remote: BackendProfile = { id: newProfileId(), name: hostLabel(url), url, apiKey: key }
    state.profiles.push(remote)
    state.activeId = remote.id
  }
  for (const k of PER_BACKEND_KEYS) {
    const value = readStorage(k)
    if (value != null) {
      writeStorage(profileStorageKey(k, state.activeId), value)
      writeStorage(k, null)
    }
  }
  writeStorage(STORAGE_KEYS.profiles, JSON.stringify(state))
  writeStorage(STORAGE_KEYS.backendUrl, null)
  writeStorage(STORAGE_KEYS.apiKey, null)
  return state
}

let idSeq = 0
export function newProfileId(): string {
  const rand = Math.random().toString(36).slice(2, 8)
  return `p-${Date.now().toString(36)}${rand}${++idSeq}`
}

/** Parse what is stored; anything unusable is dropped (remote without a valid URL, duplicates). */
function sanitize(raw: unknown): StoredProfiles | null {
  if (!raw || typeof raw !== 'object') return null
  const r = raw as { activeId?: unknown; profiles?: unknown }
  if (!Array.isArray(r.profiles)) return null
  const profiles: BackendProfile[] = []
  const seen = new Set<string>()
  for (const item of r.profiles as unknown[]) {
    if (!item || typeof item !== 'object') continue
    const p = item as Record<string, unknown>
    const id = typeof p.id === 'string' ? p.id : ''
    if (!id || seen.has(id)) continue
    const url = safeBackendUrl(typeof p.url === 'string' ? p.url : '')
    if (id !== LOCAL_PROFILE_ID && !url) continue
    seen.add(id)
    profiles.push({
      id,
      name: id === LOCAL_PROFILE_ID ? '' : typeof p.name === 'string' && p.name.trim() ? p.name.trim() : hostLabel(url),
      url,
      apiKey: typeof p.apiKey === 'string' && p.apiKey.trim() ? p.apiKey.trim() : null,
    })
  }
  if (!seen.has(LOCAL_PROFILE_ID)) profiles.unshift(localProfile())
  else profiles.sort((a, b) => Number(b.id === LOCAL_PROFILE_ID) - Number(a.id === LOCAL_PROFILE_ID))
  const activeId = typeof r.activeId === 'string' && seen.has(r.activeId) ? r.activeId : LOCAL_PROFILE_ID
  return { version: 1, activeId, profiles }
}

/** The stored profiles (migrating the legacy single URL / key on first read). Read per call. */
export function readProfiles(): StoredProfiles {
  const raw = readStorage(STORAGE_KEYS.profiles)
  if (raw == null) return migrateLegacy()
  try {
    const parsed = sanitize(JSON.parse(raw))
    if (parsed) return parsed
  } catch {
    // fall through: corrupted value
  }
  return { version: 1, activeId: LOCAL_PROFILE_ID, profiles: [localProfile()] }
}

/** Persist the profiles (no validation beyond `sanitize` on the next read) and rebuild `api`. */
export function writeProfiles(state: StoredProfiles): void {
  writeStorage(STORAGE_KEYS.profiles, JSON.stringify(state))
  rebuildApiClient()
}

export function activeProfile(): BackendProfile {
  const state = readProfiles()
  return state.profiles.find((p) => p.id === state.activeId) ?? state.profiles[0] ?? localProfile()
}

function updateActive(patch: Partial<Omit<BackendProfile, 'id'>>): void {
  const state = readProfiles()
  state.profiles = state.profiles.map((p) => (p.id === state.activeId ? { ...p, ...patch } : p))
  writeProfiles(state)
}

/** Absolute (or same-origin relative) URL for a backend path such as `/api/jobs/1/events`. */
export function apiUrl(path: string): string {
  return `${getBackendUrl()}${path}`
}

/** Headers for requests made outside `api` (SSE, audio blob downloads). */
export function authHeaders(): Record<string, string> {
  const key = getApiKey()
  return key ? { [API_KEY_HEADER]: key } : {}
}

type UnauthorizedListener = () => void
const unauthorizedListeners = new Set<UnauthorizedListener>()

/** Register a callback fired whenever the backend answers 401 (missing / wrong X-API-Key). */
export function onUnauthorized(listener: UnauthorizedListener): () => void {
  unauthorizedListeners.add(listener)
  return () => unauthorizedListeners.delete(listener)
}

/** Called by every transport (typed client, XHR uploads, SSE, blob fetches) on a 401. */
export function notifyUnauthorized(): void {
  for (const listener of unauthorizedListeners) listener()
}

type AuthorizedListener = () => void
const authorizedListeners = new Set<AuthorizedListener>()

/** Register a callback fired when an authenticated endpoint answered successfully. */
export function onAuthorized(listener: AuthorizedListener): () => void {
  authorizedListeners.add(listener)
  return () => authorizedListeners.delete(listener)
}

/** Paths the backend serves without X-API-Key: success there says nothing about the key. */
const PUBLIC_PATHS = ['/api/health']

/** Called by the transports on a 2xx from an endpoint that needs the key (clears the banner). */
export function notifyAuthorized(url?: string): void {
  if (url) {
    let path = url
    try {
      path = new URL(url, 'http://local').pathname
    } catch {
      // keep the raw string
    }
    if (PUBLIC_PATHS.some((p) => path === p || path.endsWith(p))) return
  }
  for (const listener of authorizedListeners) listener()
}

const authMiddleware: Middleware = {
  onRequest({ request }) {
    const key = getApiKey()
    if (key) request.headers.set(API_KEY_HEADER, key)
    return request
  },
  onResponse({ request, response }) {
    if (response.status === 401) notifyUnauthorized()
    else if (response.ok) notifyAuthorized(request.url)
    return response
  },
}

function createApiClient(): Client<paths> {
  const client = createClient<paths>({ baseUrl: getBackendUrl() })
  client.use(authMiddleware)
  return client
}

/** Typed client. A live binding: it is rebuilt when the backend URL changes. */
export let api: Client<paths> = createApiClient()

/** Rebuild `api` for the current active profile URL (after a switch / sidecar URL change). */
export function rebuildApiClient(): void {
  api = createApiClient()
}

/**
 * Set (or clear, with null/"") the active profile's URL and rebuild the client. Throws
 * BackendUrlError (nothing is saved) when the URL is not http(s) — or empty on a remote profile,
 * which has no default; a missing scheme becomes http.
 */
export function setBackendUrl(url: string | null): void {
  const normalized = normalizeBackendUrl(url)
  if (!normalized && activeProfile().id !== LOCAL_PROFILE_ID) {
    throw new BackendUrlError('invalid', (url ?? '').trim())
  }
  updateActive({ url: normalized })
}

/**
 * Origin (`scheme://host:port`) requests go to for a backend URL as stored ('' = this page's
 * origin, or the desktop default). Throws BackendUrlError for a URL `setBackendUrl` would reject.
 */
export function backendOrigin(url: string | null): string {
  const effective = normalizeBackendUrl(url) || getDefaultBackendUrl()
  const base = typeof window !== 'undefined' && window.location ? window.location.href : 'http://localhost/'
  return new URL(effective || '/', base).origin
}

/** The active profile's own URL, normalised (empty = the built-in profile's default applies). */
export function getBackendUrlOverride(): string {
  return activeProfile().url
}

export { normalizeBaseUrl, readStorage, safeBackendUrl, writeStorage }

/** Set (or clear) the X-API-Key of the active profile. */
export function setApiKey(key: string | null): void {
  updateActive({ apiKey: key?.trim() || null })
}
