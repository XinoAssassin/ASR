/**
 * The only module that talks to the backend. Every API call goes through `api` (typed from the
 * backend OpenAPI document, see `pnpm gen:api`) or, for non-JSON transports (SSE via
 * @microsoft/fetch-event-source, audio blobs), through `apiUrl()` + `authHeaders()`.
 *
 * Backend URL precedence: localStorage override (Settings page) → window.__ASR_CONFIG__.backendUrl
 * (config.js) → "" (same origin; the Vite dev proxy forwards /api to :8000). In the desktop app
 * the last step is http://127.0.0.1:8000 instead: the WebView origin (tauri.localhost) has no API.
 * The X-API-Key header is added from localStorage on every request; never put it in a URL (D6).
 */
import createClient, { type Client, type Middleware } from 'openapi-fetch'

import { DESKTOP_DEFAULT_BACKEND, isDesktop } from '@/platform'

import { BackendUrlError } from './errors'
import type { components, paths } from './schema'

export type Schemas = components['schemas']

export const STORAGE_KEYS = {
  backendUrl: 'asr.backendUrl',
  apiKey: 'asr.apiKey',
} as const

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
  const override = safeBackendUrl(readStorage(STORAGE_KEYS.backendUrl))
  if (override) return override
  return getDefaultBackendUrl()
}

export function getApiKey(): string | null {
  return readStorage(STORAGE_KEYS.apiKey)?.trim() || null
}

/**
 * The backend URL without the Settings override (what applies when it is cleared): config.js, else
 * same origin on the web, else the local backend in the desktop app.
 */
export function getDefaultBackendUrl(): string {
  const configured = safeBackendUrl(window.__ASR_CONFIG__?.backendUrl)
  if (configured) return configured
  return isDesktop() ? DESKTOP_DEFAULT_BACKEND : ''
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

/**
 * Persist (or clear, with null/"") the backend URL override and rebuild the client. Throws
 * BackendUrlError (nothing is saved) when the URL is not http(s); a missing scheme becomes http.
 */
export function setBackendUrl(url: string | null): void {
  const normalized = normalizeBackendUrl(url)
  writeStorage(STORAGE_KEYS.backendUrl, normalized || null)
  api = createApiClient()
}

/** The localStorage override, normalised (empty when config.js / same origin applies). */
export function getBackendUrlOverride(): string {
  return safeBackendUrl(readStorage(STORAGE_KEYS.backendUrl))
}

export { normalizeBaseUrl, readStorage, writeStorage }

/** Persist (or clear) the X-API-Key used for every request. */
export function setApiKey(key: string | null): void {
  writeStorage(STORAGE_KEYS.apiKey, key?.trim() || null)
}
