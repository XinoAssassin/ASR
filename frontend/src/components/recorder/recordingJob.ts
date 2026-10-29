/**
 * Default job for desktop recordings: the Upload page's job form ⇄ the job fields of
 * `BackendConfig` (`jobOptions`, `provider`, `termSetIds`, `extraTerms`). Pure; unit-tested.
 */
import type { ChannelsOption, JobOptions, ProviderName, Term } from '@/api/endpoints'
import { buildJobCreate, defaultJobForm, MAX_SPEAKERS, type JobFormState } from '@/utils/jobForm'
import { DEFAULT_TERM_WEIGHT, parseTerms } from '@/utils/terms'

/** Recordings are 2-track (system / mic) — PLAN §4.5 `per_channel`. */
export const DEFAULT_RECORDING_CHANNELS: ChannelsOption = 'per_channel'

export interface RecordingJobSettings {
  /** false → `jobOptions: null` (upload only). */
  createJob: boolean
  channels: ChannelsOption
  form: JobFormState
}

export function defaultRecordingJob(provider: ProviderName = 'cloud'): RecordingJobSettings {
  return { createJob: true, channels: DEFAULT_RECORDING_CHANNELS, form: defaultJobForm(provider) }
}

export interface RecordingJobConfig {
  jobOptions: JobOptions | null
  provider: ProviderName | null
  termSetIds: string[]
  extraTerms: Term[]
}

/**
 * Extra terms → the job form's text syntax (`term | alias ~~~ black,weight`), the inverse of
 * `parseTerms`. The weight is always written when the term itself contains a comma.
 */
export function formatTerms(terms: readonly Term[]): string {
  return terms
    .map((t) => {
      let line = [t.term, ...(t.aliases ?? [])].join(' | ')
      if (t.blacklist?.length) line += ` ~~~ ${t.blacklist.join(' | ')}`
      const weight = t.weight ?? DEFAULT_TERM_WEIGHT
      if (weight !== DEFAULT_TERM_WEIGHT || /[,，]/.test(line)) line += `,${weight}`
      return line
    })
    .join('\n')
}

/** Settings → the job fields of `BackendConfig`. Extra-term lines with errors are dropped. */
export function toJobConfig(settings: RecordingJobSettings): RecordingJobConfig {
  const provider = settings.form.provider
  if (!settings.createJob) return { jobOptions: null, provider, termSetIds: [], extraTerms: [] }
  const extraTerms = parseTerms(settings.form.extraTerms).terms
  const body = buildJobCreate(
    settings.form,
    { uploadId: '', channels: settings.channels, streamIndex: 0 },
    extraTerms,
  )
  return {
    jobOptions: body.options ?? null,
    provider,
    termSetIds: body.term_set_ids ?? [],
    extraTerms: body.extra_terms ?? [],
  }
}

const CHANNELS: readonly ChannelsOption[] = ['auto', 'downmix', 'left', 'right', 'per_channel']

/** The job fields of `BackendConfig` → settings (defaults for anything missing). */
export function fromJobConfig(
  config: Partial<RecordingJobConfig>,
  fallbackProvider: ProviderName = 'cloud',
): RecordingJobSettings {
  const { jobOptions, provider } = config
  const resolvedProvider = jobOptions?.provider ?? provider ?? fallbackProvider
  const settings = defaultRecordingJob(resolvedProvider)
  if (!jobOptions) return { ...settings, createJob: false }
  const form = settings.form
  form.termSetIds = [...(config.termSetIds ?? [])]
  form.extraTerms = formatTerms(config.extraTerms ?? [])
  if (Array.isArray(jobOptions.language_hints)) form.languageHints = [...jobOptions.language_hints]
  if (typeof jobOptions.num_speakers === 'number' && jobOptions.num_speakers > 0) {
    form.speakerMode = 'fixed'
    form.numSpeakers = Math.min(MAX_SPEAKERS, Math.round(jobOptions.num_speakers))
  }
  form.context = jobOptions.context ?? ''
  const pre = jobOptions.preprocess
  if (pre && CHANNELS.includes(pre.channels)) settings.channels = pre.channels
  if (typeof pre?.peak_gain === 'boolean') form.peakGain = pre.peak_gain
  if (typeof jobOptions.cloud?.keep_dialect === 'boolean') form.keepDialect = jobOptions.cloud.keep_dialect
  const corr = jobOptions.correction
  if (typeof corr?.terms === 'boolean') form.termCorrection = corr.terms
  if (typeof corr?.repetition === 'boolean') form.repetitionFilter = corr.repetition
  return settings
}
