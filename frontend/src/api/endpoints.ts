/**
 * Typed helpers over the generated client: each returns the response data or throws ApiError.
 * Stores call these; components never talk to the backend directly.
 */
import { api, normalizeBaseUrl, type Schemas } from './client'
import { errorFromResponse, toApiError, unwrap, unwrapVoid } from './errors'

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
export type TranscriptOp = Schemas['TranscriptPatch']['ops'][number]
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
export type ConnectionTestResult = Schemas['ConnectionTestResult']
export type LocalModelOut = Schemas['LocalModelOut']
export type ChannelsOption = Schemas['PreprocessOptions']['channels']
export type ChannelPolicy = ProbeInfo['channel_policy']
export type FileTransfer = Schemas['CloudSettingsOut']['file_transfer']
export type ProviderName = JobOut['provider']

// health -----------------------------------------------------------------------------------------

export const getHealth = (): Promise<HealthOut> => unwrap(api.GET('/api/health'))

/** Check a backend URL that is not saved yet (Settings → "test"). */
export async function probeHealth(baseUrl: string, timeoutMs = 8000): Promise<HealthOut> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    const response = await fetch(`${normalizeBaseUrl(baseUrl)}/api/health`, {
      signal: controller.signal,
    })
    if (!response.ok) throw await errorFromResponse(response)
    return (await response.json()) as HealthOut
  } catch (e) {
    throw toApiError(e)
  } finally {
    clearTimeout(timer)
  }
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

export const getJob = (jobId: string): Promise<JobOut> =>
  unwrap(api.GET('/api/jobs/{job_id}', { params: { path: { job_id: jobId } } }))

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

export const getTranscript = (transcriptId: string): Promise<TranscriptOut> =>
  unwrap(
    api.GET('/api/transcripts/{transcript_id}', {
      params: { path: { transcript_id: transcriptId } },
    }),
  )


export const patchTranscript = (transcriptId: string, ops: TranscriptOp[]): Promise<TranscriptOut> =>
  unwrap(
    api.PATCH('/api/transcripts/{transcript_id}', {
      params: { path: { transcript_id: transcriptId } },
      body: { ops },
    }),
  )

export const updateSpeakers = (
  transcriptId: string,
  speakers: SpeakerUpdate[],
): Promise<TranscriptOut> =>
  unwrap(
    api.PUT('/api/transcripts/{transcript_id}/speakers', {
      params: { path: { transcript_id: transcriptId } },
      body: { speakers },
    }),
  )

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

export const updateTermSet = (termSetId: string, body: TermSetIn): Promise<TermSetOut> =>
  unwrap(
    api.PUT('/api/term-sets/{term_set_id}', {
      params: { path: { term_set_id: termSetId } },
      body,
    }),
  )

export const createTermSet = (body: TermSetIn): Promise<TermSetOut> =>
  unwrap(api.POST('/api/term-sets', { body }))

// settings ---------------------------------------------------------------------------------------

export const getSettings = (): Promise<SettingsOut> => unwrap(api.GET('/api/settings'))

export const updateSettings = (body: SettingsUpdate): Promise<SettingsOut> =>
  unwrap(api.PUT('/api/settings', { body }))

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
