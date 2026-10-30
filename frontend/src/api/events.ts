/**
 * Live job updates over SSE (D6). `EventSource` cannot send X-API-Key, so this uses
 * @microsoft/fetch-event-source. The server sends `event: job` with a JobOut JSON payload on every
 * change and ends the stream after a terminal status; we reconnect with back-off otherwise.
 */
import { EventStreamContentType, fetchEventSource } from '@microsoft/fetch-event-source'

import { apiUrl, authHeaders, notifyAuthorized, notifyUnauthorized, type Schemas } from './client'
import { ApiError, errorFromResponse } from './errors'
import { isRetryableStatus } from './upload'

export type JobOut = Schemas['JobOut']
export type JobStatus = JobOut['status']

export const TERMINAL_STATUSES: readonly JobStatus[] = ['succeeded', 'failed', 'cancelled']

export function isTerminal(status: JobStatus): boolean {
  return TERMINAL_STATUSES.includes(status)
}

class FatalStreamError extends Error {
  constructor(readonly cause: ApiError) {
    super(cause.message)
  }
}

class StreamClosedError extends Error {}

export interface JobStream {
  stop(): void
  /** Settles when the stream ends: after a terminal status, stop(), or a fatal error (rejects). */
  done: Promise<void>
}

export interface WatchJobOptions {
  onJob: (job: JobOut) => void
  /** Called on a non-retryable error (401, 404, ...); the stream is over. */
  onError?: (error: ApiError) => void
  minRetryMs?: number
  maxRetryMs?: number
}

export function watchJob(jobId: string, options: WatchJobOptions): JobStream {
  const { onJob, onError, minRetryMs = 1000, maxRetryMs = 30000 } = options
  const controller = new AbortController()
  let terminal = false
  let retryMs = minRetryMs

  const done = fetchEventSource(apiUrl(`/api/jobs/${encodeURIComponent(jobId)}/events`), {
    headers: authHeaders(),
    signal: controller.signal,
    // The library's default closes the stream when the tab is hidden and reopens it when shown;
    // that reopen is not tied to our controller's lifetime and can orphan a second connection.
    openWhenHidden: true,
    async onopen(response) {
      const type = response.headers.get('content-type') ?? ''
      if (response.ok && type.startsWith(EventStreamContentType)) {
        retryMs = minRetryMs
        notifyAuthorized()
        return
      }
      if (response.status === 401) notifyUnauthorized()
      const error = await errorFromResponse(response)
      if (response.ok || !isRetryableStatus(response.status)) throw new FatalStreamError(error)
      throw error
    },
    onmessage(ev) {
      if ((ev.event && ev.event !== 'job') || !ev.data) return
      let job: JobOut
      try {
        job = JSON.parse(ev.data) as JobOut
      } catch {
        return
      }
      onJob(job)
      if (isTerminal(job.status)) {
        terminal = true
        controller.abort()
      }
    },
    onclose() {
      // The server closes after a terminal status; anything else is a dropped connection.
      if (!terminal) throw new StreamClosedError()
    },
    onerror(err) {
      if (err instanceof FatalStreamError) throw err
      const wait = retryMs
      retryMs = Math.min(retryMs * 2, maxRetryMs)
      return wait
    },
  }).catch((err: unknown) => {
    if (err instanceof FatalStreamError) {
      onError?.(err.cause)
      return
    }
    throw err
  })

  return {
    stop: () => controller.abort(),
    done,
  }
}
