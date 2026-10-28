/** Job form state → `JobCreate` request body (pure; unit-tested). */
import type { ChannelsOption, JobCreate, ProviderName, Term } from '@/api/endpoints'

export const LANGUAGE_OPTIONS = ['zh', 'en'] as const
export const MAX_SPEAKERS = 20
export const MAX_CONTEXT_LENGTH = 2000

export interface JobFormState {
  provider: ProviderName
  languageHints: string[]
  speakerMode: 'auto' | 'fixed'
  numSpeakers: number
  termSetIds: string[]
  extraTerms: string
  context: string
  keepDialect: boolean
}

export function defaultJobForm(provider: ProviderName = 'cloud'): JobFormState {
  return {
    provider,
    languageHints: ['zh', 'en'],
    speakerMode: 'auto',
    numSpeakers: 2,
    termSetIds: [],
    extraTerms: '',
    context: '',
    keepDialect: false,
  }
}

export interface JobTarget {
  uploadId: string
  channels: ChannelsOption
  streamIndex: number
}

export function buildJobCreate(form: JobFormState, target: JobTarget, extraTerms: Term[]): JobCreate {
  const context = form.context.trim().slice(0, MAX_CONTEXT_LENGTH)
  const numSpeakers =
    form.speakerMode === 'fixed'
      ? Math.min(MAX_SPEAKERS, Math.max(1, Math.round(form.numSpeakers)))
      : null
  return {
    upload_id: target.uploadId,
    provider: form.provider,
    term_set_ids: [...form.termSetIds],
    extra_terms: extraTerms,
    options: {
      provider: form.provider,
      language_hints: [...form.languageHints],
      num_speakers: numSpeakers,
      context: context || null,
      preprocess: { channels: target.channels, stream_index: target.streamIndex, peak_gain: true },
      cloud: { keep_dialect: form.keepDialect },
    },
  }
}
