/**
 * Uniform error type for every backend call. FastAPI answers `{"detail": ...}` where detail is a
 * string (HTTPException) or a list of validation errors (422). Transport failures (backend down,
 * CORS, DNS) surface as status 0.
 */
export class ApiError extends Error {
  readonly status: number
  readonly detail: string | null

  constructor(status: number, detail: string | null, message?: string) {
    super(message ?? detail ?? (status ? `HTTP ${status}` : 'Network error'))
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }

  get isNetwork(): boolean {
    return this.status === 0
  }

  get isUnauthorized(): boolean {
    return this.status === 401
  }

  /** 409: the resource changed since the client read it (`expected_updated_at` mismatch). */
  get isConflict(): boolean {
    return this.status === 409
  }
}

/**
 * A backend URL that cannot be used: `invalid` (not a URL even with `http://` in front) or
 * `scheme` (a scheme other than http / https). Thrown by `setBackendUrl` / `normalizeBackendUrl`.
 */
export class BackendUrlError extends Error {
  readonly reason: 'invalid' | 'scheme'
  readonly input: string

  constructor(reason: 'invalid' | 'scheme', input: string) {
    super(reason === 'scheme' ? `Unsupported URL scheme: ${input}` : `Invalid URL: ${input}`)
    this.name = 'BackendUrlError'
    this.reason = reason
    this.input = input
  }
}

interface ValidationItem {
  loc?: (string | number)[]
  msg?: string
}

/** Flatten a FastAPI error body into one human-readable line (null when there is nothing useful). */
export function extractDetail(body: unknown): string | null {
  if (body == null) return null
  if (typeof body === 'string') return body.trim() || null
  if (typeof body !== 'object') return String(body)
  const detail = (body as { detail?: unknown }).detail
  if (detail == null) return null
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const parts = detail.map((item: ValidationItem | string) => {
      if (typeof item === 'string') return item
      const loc = (item.loc ?? []).filter((p) => p !== 'body' && p !== 'query' && p !== 'path')
      return loc.length ? `${loc.join('.')}: ${item.msg ?? ''}` : (item.msg ?? '')
    })
    return parts.filter(Boolean).join('; ') || null
  }
  try {
    return JSON.stringify(detail)
  } catch {
    return String(detail)
  }
}

/** Build an ApiError from a non-OK fetch Response (reads the body once). */
export async function errorFromResponse(response: Response): Promise<ApiError> {
  let body: unknown = null
  try {
    const text = await response.text()
    try {
      body = JSON.parse(text)
    } catch {
      body = text
    }
  } catch {
    body = null
  }
  return new ApiError(response.status, extractDetail(body))
}

export function toApiError(e: unknown): ApiError {
  if (e instanceof ApiError) return e
  if (e instanceof DOMException && e.name === 'AbortError') return new ApiError(-1, null, 'Aborted')
  return new ApiError(0, null, e instanceof Error ? e.message : String(e))
}

export function isAbortError(e: unknown): boolean {
  return (
    (e instanceof DOMException && e.name === 'AbortError') ||
    (e instanceof ApiError && e.status === -1)
  )
}

interface FetchResult<T> {
  data?: T
  error?: unknown
  response: Response
}

/**
 * Await an openapi-fetch call and return its data, or throw ApiError. Network failures (the
 * promise itself rejects) become ApiError(0).
 */
export async function unwrap<T>(call: Promise<FetchResult<T>>): Promise<T> {
  let result: FetchResult<T>
  try {
    result = await call
  } catch (e) {
    throw toApiError(e)
  }
  if (!result.response.ok) {
    throw new ApiError(result.response.status, extractDetail(result.error))
  }
  return result.data as T
}

/** Like `unwrap` for endpoints without a body (204). */
export async function unwrapVoid(call: Promise<FetchResult<unknown>>): Promise<void> {
  await unwrap(call)
}
