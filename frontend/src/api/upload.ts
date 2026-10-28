/**
 * Chunked upload (D11): `POST /api/uploads/init` → `PUT /api/uploads/{session}/parts/{n}` for
 * every part, sequentially, with retry → `POST /api/uploads/{session}/complete`.
 * Parts go through XMLHttpRequest because fetch has no upload-progress events.
 */
import { api, apiUrl, authHeaders, notifyUnauthorized, type Schemas } from './client'
import { ApiError, extractDetail, unwrap } from './errors'

export type UploadOut = Schemas['UploadOut']

export interface UploadProgress {
  /** Bytes acknowledged or in flight for the whole file. */
  loaded: number
  total: number
  /** Zero-based index of the part being sent. */
  part: number
  totalParts: number
}

export interface ChunkedUploadOptions {
  onProgress?: (progress: UploadProgress) => void
  signal?: AbortSignal
  /** Attempts per part (first try included). */
  attempts?: number
  /** Base back-off in ms; doubles per retry. */
  retryDelayMs?: number
}

/** Transient failures worth retrying: network, timeouts, rate limits and 5xx (not 501). */
export function isRetryableStatus(status: number): boolean {
  return status === 0 || status === 408 || status === 429 || (status >= 500 && status !== 501)
}

function abortError(): DOMException {
  return new DOMException('Aborted', 'AbortError')
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(abortError())
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    const onAbort = (): void => {
      clearTimeout(timer)
      reject(abortError())
    }
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

function putPart(
  url: string,
  body: Blob,
  onProgress: (loaded: number) => void,
  signal?: AbortSignal,
): Promise<Schemas['UploadPartOut']> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(abortError())
    const xhr = new XMLHttpRequest()
    xhr.open('PUT', url)
    xhr.setRequestHeader('Content-Type', 'application/octet-stream')
    for (const [name, value] of Object.entries(authHeaders())) xhr.setRequestHeader(name, value)
    xhr.upload.onprogress = (ev) => onProgress(ev.loaded)
    const onAbort = (): void => xhr.abort()
    signal?.addEventListener('abort', onAbort, { once: true })
    const cleanup = (): void => signal?.removeEventListener('abort', onAbort)
    xhr.onload = () => {
      cleanup()
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText) as Schemas['UploadPartOut'])
        } catch {
          resolve({ n: -1, size: body.size })
        }
        return
      }
      if (xhr.status === 401) notifyUnauthorized()
      let parsed: unknown = xhr.responseText
      try {
        parsed = JSON.parse(xhr.responseText)
      } catch {
        // plain-text body
      }
      reject(new ApiError(xhr.status, extractDetail(parsed)))
    }
    xhr.onerror = () => {
      cleanup()
      reject(new ApiError(0, null))
    }
    xhr.onabort = () => {
      cleanup()
      reject(abortError())
    }
    xhr.send(body)
  })
}

export async function uploadFileChunked(
  file: File,
  targetDir: string,
  options: ChunkedUploadOptions = {},
): Promise<UploadOut> {
  const { onProgress, signal, attempts = 4, retryDelayMs = 1000 } = options
  const init = await unwrap(
    api.POST('/api/uploads/init', {
      body: { filename: file.name, size: file.size, target_dir: targetDir },
      signal,
    }),
  )
  const { session_id: sessionId, part_size: partSize, total_parts: totalParts } = init
  const report = (loaded: number, part: number): void =>
    onProgress?.({ loaded: Math.min(loaded, file.size), total: file.size, part, totalParts })
  report(0, 0)

  for (let n = 0; n < totalParts; n++) {
    const start = n * partSize
    const chunk = file.slice(start, Math.min(start + partSize, file.size))
    const url = apiUrl(
      `/api/uploads/${encodeURIComponent(sessionId)}/parts/${encodeURIComponent(String(n))}`,
    )
    for (let attempt = 1; ; attempt++) {
      try {
        await putPart(url, chunk, (loaded) => report(start + loaded, n), signal)
        break
      } catch (e) {
        if (!(e instanceof ApiError) || !isRetryableStatus(e.status) || attempt >= attempts) throw e
        report(start, n)
        await sleep(retryDelayMs * 2 ** (attempt - 1), signal)
      }
    }
    report(start + chunk.size, n)
  }

  return unwrap(
    api.POST('/api/uploads/{session_id}/complete', {
      params: { path: { session_id: sessionId } },
      signal,
    }),
  )
}
