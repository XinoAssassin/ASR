/**
 * The only module that talks to the backend. Every API call goes through `api` (typed from the
 * backend OpenAPI document, see `pnpm gen:api`) or, for non-JSON transports (SSE via
 * @microsoft/fetch-event-source, audio blobs), through `apiUrl()` + `authHeaders()`.
 *
 * Backend URL precedence: localStorage override (Settings page) → window.__ASR_CONFIG__.backendUrl
 * (config.js) → "" (same origin; the Vite dev proxy forwards /api to :8000).
 * The X-API-Key header is added from localStorage on every request; never put it in a URL (D6).
 */
import createClient, { type Client, type Middleware } from 'openapi-fetch'

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

export function getBackendUrl(): string {
  const override = normalizeBaseUrl(readStorage(STORAGE_KEYS.backendUrl))
  if (override) return override
  return normalizeBaseUrl(window.__ASR_CONFIG__?.backendUrl)
}

export function getApiKey(): string | null {
  return readStorage(STORAGE_KEYS.apiKey)?.trim() || null
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

const authMiddleware: Middleware = {
  onRequest({ request }) {
    const key = getApiKey()
    if (key) request.headers.set(API_KEY_HEADER, key)
    return request
  },
}

function createApiClient(): Client<paths> {
  const client = createClient<paths>({ baseUrl: getBackendUrl() })
  client.use(authMiddleware)
  return client
}

/** Typed client. A live binding: it is rebuilt when the backend URL changes. */
export let api: Client<paths> = createApiClient()

/** Persist (or clear, with null/"") the backend URL override and rebuild the client. */
export function setBackendUrl(url: string | null): void {
  writeStorage(STORAGE_KEYS.backendUrl, normalizeBaseUrl(url) || null)
  api = createApiClient()
}

/** Persist (or clear) the X-API-Key used for every request. */
export function setApiKey(key: string | null): void {
  writeStorage(STORAGE_KEYS.apiKey, key?.trim() || null)
}
