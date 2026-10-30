/**
 * Typed helpers over the generated client: each returns the response data or throws ApiError.
 * Anything that changes server state or is shared between pages goes through a store. The only
 * direct calls from components are read-only lookups owned by that one component: the directory
 * tree of DirectoryPicker (`listRoots` / `listDirs`), the Settings page probing a backend URL
 * before it is saved (`probeHealth`) and the SiYuan dialog reading the upload's file name for its
 * path preview (`getUpload`); a store around them would hold no state.
 */
import { api, API_KEY_HEADER, normalizeBackendUrl, type Schemas } from './client'
import {
  ApiError,
  errorFromResponse,
  extractDetail,
  toApiError,
  unwrap,
  unwrapVoid,
} from './errors'

export type HealthOut = Schemas['HealthOut']
export type FsRoot = Schemas['FsRoot']
export type FsDir = Schemas['FsDir']
export type FsDirsOut = Schemas['FsDirsOut']
export type UploadOut = Schemas['UploadOut']
export type ProbeInfo = Schemas['ProbeInfo']
export type JobCreate = Schemas['JobCreate']
export type JobOut = Schemas['JobOut']
export type JobList = Schemas['JobList']
export type JobOptions = Schemas['JobOptions']
export type JobStatus = JobOut['status']
export type TranscriptOut = Schemas['TranscriptOut']
type SchemaTranscriptOp = Schemas['TranscriptPatch']['ops'][number]
/** `ignore_case` has a server default (false), so it is optional when building the op. */
export type ReplaceOp = Omit<Schemas['ReplaceOp'], 'ignore_case'> & { ignore_case?: boolean }
/** `at` is a code-point index (see `utf16ToCodePoint` in `@/utils/text`). */
export type SplitOp = Schemas['SplitOp']
export type TranscriptOp = Exclude<SchemaTranscriptOp, { op: 'replace' }> | ReplaceOp
export type Correction = Schemas['Correction']
export type SpeakerUpdate = Schemas['SpeakerUpdate']
export type LlmSuggestionsOut = Schemas['LlmSuggestionsOut']
export type TranscriptSummary = Schemas['TranscriptSummary']
export type ActionItem = Schemas['ActionItem']
export type TermSetIn = Schemas['TermSetIn']
export type Segment = Schemas['Segment']
export type Speaker = Schemas['Speaker']
export type Term = Schemas['Term']
export type TermSetOut = Schemas['TermSetOut']
export type SettingsOut = Schemas['SettingsOut']
export type SettingsUpdate = Schemas['SettingsUpdate']
export type LlmSettingsOut = Schemas['LlmSettingsOut']
export type LlmSettingsUpdate = Schemas['LlmSettingsUpdate']
/** Provider of the LLM pass (summary, L3 suggestions): DashScope or any OpenAI-compatible server. */
export type LlmProviderName = LlmSettingsOut['provider']
/** Where an effective value comes from; precedence settings (UI) > config > env > default. */
export type SettingSource = SettingsOut['sources'][string]
/** Keys of `SettingsOut.sources` (the backend types it as a plain string map). */
export type SettingSourceKey =
  | 'cloud.endpoint'
  | 'cloud.model'
  | 'cloud.file_transfer'
  | 'cloud.api_key'
  | 'llm.provider'
  | 'llm.endpoint'
  | 'llm.model'
  | 'llm.api_key'
export type ConfigFileStatus = Schemas['ConfigFileOut']
export type LlmTestResult = Schemas['LlmTestOut']
export type ConnectionTestResult = Schemas['ConnectionTestResult']
export type LocalModelOut = Schemas['LocalModelOut']
export type ChannelsOption = Schemas['PreprocessOptions']['channels']
export type ChannelPolicy = ProbeInfo['channel_policy']
export type FileTransfer = Schemas['CloudSettingsOut']['file_transfer']
export type ProviderName = JobOut['provider']
export type EnvironmentOut = Schemas['EnvironmentOut']
export type SiYuanSettingsOut = Schemas['SiYuanSettingsOut']
export type SiYuanSettingsUpdate = Schemas['SiYuanSettingsUpdate']
export type SiYuanTestOut = Schemas['SiYuanTestOut']
export type SiYuanNotebookOut = Schemas['SiYuanNotebookOut']
export type SiYuanPushIn = Schemas['SiYuanPushIn']
export type SiYuanPushOut = Schemas['SiYuanPushOut']
export type ExportLocale = NonNullable<SiYuanPushIn['locale']>

/** Compute device of the local pipeline (`rocm`: AMD GPU through torch's HIP build). */
export type DeviceId = Schemas['HardwareDeviceOut']['id']
export type HardwareDeviceOut = Schemas['HardwareDeviceOut']
export type HardwareOut = Schemas['HardwareOut']

// health -----------------------------------------------------------------------------------------

export const getHealth = (): Promise<HealthOut> => unwrap(api.GET('/api/health'))

/** Check a backend URL that is not saved yet (Settings → "test"). */
export async function probeHealth(baseUrl: string, timeoutMs = 8000): Promise<HealthOut> {
  // Throws BackendUrlError (not an ApiError) for an unusable URL, before any request.
  const url = `${normalizeBackendUrl(baseUrl)}/api/health`
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    const response = await fetch(url, { signal: controller.signal })
    if (!response.ok) throw await errorFromResponse(response)
    return (await response.json()) as HealthOut
  } catch (e) {
    throw toApiError(e)
  } finally {
    clearTimeout(timer)
  }
}

/**
 * Check that `apiKey` is accepted by the backend at `baseUrl` (an authenticated read). Resolves
 * true on 2xx, false on 401; throws ApiError otherwise. Like `probeHealth`, for unsaved profiles.
 */
export async function probeAuth(baseUrl: string, apiKey: string | null, timeoutMs = 8000): Promise<boolean> {
  const url = `${normalizeBackendUrl(baseUrl)}/api/term-sets`
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    const headers: Record<string, string> = apiKey ? { [API_KEY_HEADER]: apiKey } : {}
    const response = await fetch(url, { signal: controller.signal, headers })
    if (response.status === 401) return false
    if (!response.ok) throw await errorFromResponse(response)
    return true
  } catch (e) {
    throw toApiError(e)
  } finally {
    clearTimeout(timer)
  }
}

// system -----------------------------------------------------------------------------------------

/** Devices the backend can run the local pipeline on; null when the backend predates the endpoint. */
export async function getHardware(): Promise<HardwareOut | null> {
  let result
  try {
    result = await api.GET('/api/system/hardware')
  } catch (e) {
    throw toApiError(e)
  }
  if (result.response.status === 404) return null
  if (!result.response.ok) throw new ApiError(result.response.status, extractDetail(result.error))
  return result.data as HardwareOut
}

// filesystem / uploads ---------------------------------------------------------------------------

export const listRoots = (): Promise<FsRoot[]> => unwrap(api.GET('/api/fs/roots'))

export const listDirs = (path: string): Promise<FsDirsOut> =>
  unwrap(api.GET('/api/fs/dirs', { params: { query: { path } } }))

export const registerUpload = (path: string): Promise<UploadOut> =>
  unwrap(api.POST('/api/uploads/register', { body: { path } }))

export const getUpload = (uploadId: string): Promise<UploadOut> =>
  unwrap(api.GET('/api/uploads/{upload_id}', { params: { path: { upload_id: uploadId } } }))

// jobs -------------------------------------------------------------------------------------------

export const createJob = (body: JobCreate): Promise<JobOut> =>
  unwrap(api.POST('/api/jobs', { body }))

export const listJobs = (query: {
  page: number
  page_size: number
  status?: JobStatus | null
}): Promise<JobList> => unwrap(api.GET('/api/jobs', { params: { query } }))

export const getJob = (jobId: string, signal?: AbortSignal): Promise<JobOut> =>
  unwrap(api.GET('/api/jobs/{job_id}', { params: { path: { job_id: jobId } }, signal }))

export const cancelJob = (jobId: string): Promise<JobOut> =>
  unwrap(api.POST('/api/jobs/{job_id}/cancel', { params: { path: { job_id: jobId } } }))

export const retryJob = (jobId: string): Promise<JobOut> =>
  unwrap(api.POST('/api/jobs/{job_id}/retry', { params: { path: { job_id: jobId } } }))

export const deleteJob = (jobId: string, deleteAudio: boolean): Promise<void> =>
  unwrapVoid(
    api.DELETE('/api/jobs/{job_id}', {
      params: { path: { job_id: jobId }, query: { delete_audio: deleteAudio } },
    }),
  )

// transcripts ------------------------------------------------------------------------------------

export const getTranscript = (transcriptId: string, signal?: AbortSignal): Promise<TranscriptOut> =>
  unwrap(
    api.GET('/api/transcripts/{transcript_id}', {
      params: { path: { transcript_id: transcriptId } },
      signal,
    }),
  )

export interface WriteOptions {
  /**
   * `updated_at` of the transcript the change was based on; the backend answers 409 when the
   * stored transcript is newer (edited elsewhere). Omitted / null: no check.
   */
  expectedUpdatedAt?: string | null
  /** Let the request outlive the page (pagehide flush). */
  keepalive?: boolean
  signal?: AbortSignal
}

function writeInit(options: WriteOptions): { keepalive?: boolean; signal?: AbortSignal } {
  const init: { keepalive?: boolean; signal?: AbortSignal } = {}
  if (options.keepalive) init.keepalive = true
  if (options.signal) init.signal = options.signal
  return init
}

export function patchTranscript(
  transcriptId: string,
  ops: TranscriptOp[],
  options: WriteOptions = {},
): Promise<TranscriptOut> {
  // Ops whose fields have server defaults (ReplaceOp.ignore_case) may omit them.
  const body: Schemas['TranscriptPatch'] = { ops: ops as SchemaTranscriptOp[] }
  if (options.expectedUpdatedAt) body.expected_updated_at = options.expectedUpdatedAt
  return unwrap(
    api.PATCH('/api/transcripts/{transcript_id}', {
      params: { path: { transcript_id: transcriptId } },
      body,
      ...writeInit(options),
    }),
  )
}

export function updateSpeakers(
  transcriptId: string,
  speakers: SpeakerUpdate[],
  options: WriteOptions = {},
): Promise<TranscriptOut> {
  const body: Schemas['SpeakersUpdate'] = { speakers }
  if (options.expectedUpdatedAt) body.expected_updated_at = options.expectedUpdatedAt
  return unwrap(
    api.PUT('/api/transcripts/{transcript_id}/speakers', {
      params: { path: { transcript_id: transcriptId } },
      body,
      ...writeInit(options),
    }),
  )
}

export const revertTranscript = (transcriptId: string): Promise<TranscriptOut> =>
  unwrap(
    api.POST('/api/transcripts/{transcript_id}/revert', {
      params: { path: { transcript_id: transcriptId } },
    }),
  )

export const requestLlmSuggestions = (
  transcriptId: string,
  segmentIds: string[] | null,
): Promise<LlmSuggestionsOut> =>
  unwrap(
    api.POST('/api/transcripts/{transcript_id}/llm-suggestions', {
      params: { path: { transcript_id: transcriptId } },
      body: { segment_ids: segmentIds },
    }),
  )

/** Opt-in LLM summary (costs money, sends the text to DashScope); replaces any previous one. */
export const createSummary = (transcriptId: string): Promise<TranscriptSummary> =>
  unwrap(
    api.POST('/api/transcripts/{transcript_id}/summary', {
      params: { path: { transcript_id: transcriptId } },
    }),
  )

export const deleteSummary = (transcriptId: string): Promise<void> =>
  unwrapVoid(
    api.DELETE('/api/transcripts/{transcript_id}/summary', {
      params: { path: { transcript_id: transcriptId } },
    }),
  )

// term sets --------------------------------------------------------------------------------------

export const listTermSets = (): Promise<TermSetOut[]> => unwrap(api.GET('/api/term-sets'))

export const getTermSet = (termSetId: string): Promise<TermSetOut> =>
  unwrap(api.GET('/api/term-sets/{term_set_id}', { params: { path: { term_set_id: termSetId } } }))

export const updateTermSet = (termSetId: string, body: TermSetIn): Promise<TermSetOut> =>
  unwrap(
    api.PUT('/api/term-sets/{term_set_id}', {
      params: { path: { term_set_id: termSetId } },
      body,
    }),
  )

export const createTermSet = (body: TermSetIn): Promise<TermSetOut> =>
  unwrap(api.POST('/api/term-sets', { body }))

export const deleteTermSet = (termSetId: string): Promise<void> =>
  unwrapVoid(
    api.DELETE('/api/term-sets/{term_set_id}', { params: { path: { term_set_id: termSetId } } }),
  )

/** One rejected line of a term import (1-based line number, message, the raw line). */
export interface TermImportLineError {
  line: number
  message: string
  input: string | null
}

/**
 * 422 from `POST /api/term-sets/import` with per-line errors (`loc: ["body", "text", <line>]`).
 * Other 422s (blank name, too many terms) stay plain ApiErrors.
 */
export class TermImportError extends ApiError {
  readonly lines: TermImportLineError[]

  constructor(status: number, detail: string | null, lines: TermImportLineError[]) {
    super(status, detail)
    this.name = 'TermImportError'
    this.lines = lines
  }
}

interface ImportErrorItem {
  loc?: unknown[]
  msg?: unknown
  input?: unknown
}

/** The per-line errors of an import 422 body, or [] when the body has none. */
export function importLineErrors(body: unknown): TermImportLineError[] {
  const detail = (body as { detail?: unknown } | null)?.detail
  if (!Array.isArray(detail)) return []
  const out: TermImportLineError[] = []
  for (const item of detail as ImportErrorItem[]) {
    const loc = Array.isArray(item?.loc) ? item.loc : []
    const line = loc[0] === 'body' && loc[1] === 'text' ? loc[2] : undefined
    if (typeof line !== 'number') continue
    const msg = typeof item.msg === 'string' ? item.msg : ''
    out.push({
      line,
      // The backend prefixes "line N: "; the UI shows the line number in its own column.
      message: msg.replace(/^line \d+:\s*/, ''),
      input: typeof item.input === 'string' ? item.input : null,
    })
  }
  return out
}

/** Create a term set from pasted text / a .txt file. Throws TermImportError for bad lines. */
export async function importTermSet(name: string, text: string): Promise<TermSetOut> {
  let result
  try {
    result = await api.POST('/api/term-sets/import', { body: { name, text } })
  } catch (e) {
    throw toApiError(e)
  }
  if (!result.response.ok) {
    const lines = result.response.status === 422 ? importLineErrors(result.error) : []
    const detail = extractDetail(result.error)
    if (lines.length) throw new TermImportError(result.response.status, detail, lines)
    throw new ApiError(result.response.status, detail)
  }
  return result.data as TermSetOut
}

// settings ---------------------------------------------------------------------------------------

export const getSettings = (): Promise<SettingsOut> => unwrap(api.GET('/api/settings'))

export const updateSettings = (body: SettingsUpdate): Promise<SettingsOut> =>
  unwrap(api.PUT('/api/settings', { body }))

/** Check the saved LLM settings (always 200; `ok` says whether the server answered). */
export const testLlm = (): Promise<LlmTestResult> => unwrap(api.POST('/api/settings/test-llm'))

export const testCloud = (): Promise<ConnectionTestResult> =>
  unwrap(api.POST('/api/settings/test-cloud'))

export const listLocalModels = (): Promise<LocalModelOut[]> =>
  unwrap(api.GET('/api/settings/local-models'))

export const downloadLocalModel = (key: string, mirror: boolean): Promise<LocalModelOut> =>
  unwrap(
    api.POST('/api/settings/local-models/{key}/download', {
      params: { path: { key }, query: { mirror } },
    }),
  )

// SiYuan (思源笔记) export --------------------------------------------------------------------------

export const getSiyuanSettings = (): Promise<SiYuanSettingsOut> =>
  unwrap(api.GET('/api/siyuan/settings'))

export const updateSiyuanSettings = (body: SiYuanSettingsUpdate): Promise<SiYuanSettingsOut> =>
  unwrap(api.PUT('/api/siyuan/settings', { body }))

export const testSiyuan = (): Promise<SiYuanTestOut> => unwrap(api.POST('/api/siyuan/test'))

/** 422 when SiYuan is not configured, 502 when the backend cannot reach it. */
export const listSiyuanNotebooks = (): Promise<SiYuanNotebookOut[]> =>
  unwrap(api.GET('/api/siyuan/notebooks'))

export const pushToSiyuan = (transcriptId: string, body: SiYuanPushIn): Promise<SiYuanPushOut> =>
  unwrap(
    api.POST('/api/transcripts/{transcript_id}/siyuan', {
      params: { path: { transcript_id: transcriptId } },
      body,
    }),
  )
