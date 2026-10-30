/**
 * Authenticated binary fetches (D6): audio for the waveform and transcript exports. The API key
 * travels in the X-API-Key header; it is never put into a URL, so `<audio src>` / `<a href>` to the
 * backend are not used.
 */
import { parseContentDispositionFilename } from '@/utils/contentDisposition'

import { apiUrl, authHeaders, notifyAuthorized, notifyUnauthorized } from './client'
import { errorFromResponse, toApiError } from './errors'

export interface BlobResult {
  blob: Blob
  filename: string | null
}

export interface DownloadProgress {
  loaded: number
  /** From Content-Length; null when the server did not send one. */
  total: number | null
}

export interface FetchBlobOptions {
  signal?: AbortSignal
  /** Called as the body arrives (read through `response.body`, so progress can be shown). */
  onProgress?: (progress: DownloadProgress) => void
}

async function readBody(response: Response, onProgress: FetchBlobOptions['onProgress']): Promise<Blob> {
  const type = response.headers.get('content-type') ?? ''
  if (!onProgress || !response.body) return response.blob()
  const length = Number(response.headers.get('content-length'))
  const total = Number.isFinite(length) && length > 0 ? length : null
  const reader = response.body.getReader()
  const chunks: Uint8Array[] = []
  let loaded = 0
  onProgress({ loaded, total })
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    chunks.push(value)
    loaded += value.byteLength
    onProgress({ loaded, total })
  }
  return new Blob(chunks as BlobPart[], { type })
}

export async function fetchBlob(
  path: string,
  options: AbortSignal | FetchBlobOptions = {},
): Promise<BlobResult> {
  const { signal, onProgress } = options instanceof AbortSignal ? { signal: options } : options
  let response: Response
  try {
    response = await fetch(apiUrl(path), { headers: authHeaders(), signal })
  } catch (e) {
    throw toApiError(e)
  }
  if (!response.ok) {
    if (response.status === 401) notifyUnauthorized()
    throw await errorFromResponse(response)
  }
  notifyAuthorized()
  let blob: Blob
  try {
    blob = await readBody(response, onProgress)
  } catch (e) {
    throw toApiError(e)
  }
  return {
    blob,
    filename: parseContentDispositionFilename(response.headers.get('content-disposition')),
  }
}

/** Hand a Blob to the browser as a download. */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.rel = 'noopener'
  a.style.display = 'none'
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 60_000)
}

export function audioPath(uploadId: string): string {
  return `/api/audio/${encodeURIComponent(uploadId)}`
}

export type ExportFormat = 'txt' | 'srt' | 'vtt' | 'json'

export interface ExportOptions {
  format: ExportFormat
  timestamps: boolean
  mergeSameSpeaker: boolean
}

export function exportPath(transcriptId: string, options: ExportOptions): string {
  const query = new URLSearchParams({
    format: options.format,
    timestamps: String(options.timestamps),
    merge_same_speaker: String(options.mergeSameSpeaker),
  })
  return `/api/transcripts/${encodeURIComponent(transcriptId)}/export?${query.toString()}`
}

/** Download an export; returns the filename used. */
export async function downloadExport(
  transcriptId: string,
  options: ExportOptions,
): Promise<string> {
  const { blob, filename } = await fetchBlob(exportPath(transcriptId, options))
  const name = filename ?? `transcript-${transcriptId}.${options.format}`
  saveBlob(blob, name)
  return name
}
