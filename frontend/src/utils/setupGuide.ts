/**
 * First-run setup guide: pure readiness checks (unit-tested). The store (`stores/setupGuide.ts`)
 * feeds them the live state of the active backend.
 */
import type { LocalModelOut, ProviderName, SettingsOut } from '@/api/endpoints'

/** ASR models the server's "auto" choice picks from (M4: 1.7B with native BF16, else 0.6B). */
export const AUTO_ASR_MODELS = ['qwen3-asr-0.6b', 'qwen3-asr-1.7b'] as const

/** Engines that give their own timestamps (no forced aligner): Paraformer, Whisper. */
function needsAligner(asrModelId: string): boolean {
  return !/paraformer|whisper/i.test(asrModelId)
}

export interface RecognitionState {
  /** Settings and the model list have been read. */
  loaded: boolean
  /** A DashScope key is configured on the server. */
  cloudReady: boolean
  /** Every model a local job needs is downloaded. */
  localReady: boolean
  /** The backend lists local models at all (an older / cloud-only backend may not). */
  localAvailable: boolean
  /** What a local job still needs (one entry per missing role). */
  missing: LocalModelOut[]
  /** Server default provider. */
  defaultProvider: ProviderName | null
  /** Provider the sample job uses: the default when ready, else the ready one, else null. */
  sampleProvider: ProviderName | null
}

/**
 * Models a local job loads: VAD, the ASR model (configured, or for "auto" either Qwen3-ASR size),
 * the forced aligner when that engine needs one, and the diarization model. Mirrors
 * `asr.providers.local.models.required_models` as far as the API exposes it.
 */
export function missingLocalModels(
  models: readonly LocalModelOut[],
  asrModel: string | null | undefined,
  diarizeModel: string | null | undefined,
): LocalModelOut[] {
  const missing: LocalModelOut[] = []
  const ofKind = (kind: LocalModelOut['kind']) => models.filter((m) => m.kind === kind)
  const need = (candidates: LocalModelOut[], preferred?: string | null): void => {
    const chosen = preferred ? candidates.filter((m) => m.id === preferred) : candidates
    if (!chosen.length) return // not listed: nothing the user could download here
    if (!chosen.some((m) => m.downloaded)) missing.push(chosen[0]!)
  }

  need(ofKind('vad'))
  let asrId: string | null = null
  if (asrModel) {
    need(ofKind('asr'), asrModel)
    asrId = asrModel
  } else {
    // "auto": either size counts; suggest the smaller one when none is there.
    const auto = ofKind('asr').filter((m) => (AUTO_ASR_MODELS as readonly string[]).includes(m.id))
    need(auto)
    asrId = auto.find((m) => m.downloaded)?.id ?? auto[0]?.id ?? null
  }
  if (asrId && needsAligner(asrId)) need(ofKind('aligner'))
  need(ofKind('diarization'), diarizeModel)
  return missing
}

export function recognitionState(
  server: SettingsOut | null,
  models: readonly LocalModelOut[],
  modelsLoaded: boolean,
): RecognitionState {
  const cloudReady = !!server?.cloud.api_key_set
  const localAvailable = models.some((m) => m.kind === 'asr')
  const missing = server && localAvailable ? missingLocalModels(models, server.local.asr_model, server.local.diarize_model) : []
  const localReady = !!server && localAvailable && missing.length === 0
  const defaultProvider = server?.provider_default ?? null
  const ready = (p: ProviderName): boolean => (p === 'cloud' ? cloudReady : localReady)
  let sampleProvider: ProviderName | null = null
  if (defaultProvider && ready(defaultProvider)) sampleProvider = defaultProvider
  else if (localReady) sampleProvider = 'local'
  else if (cloudReady) sampleProvider = 'cloud'
  return {
    loaded: !!server && modelsLoaded,
    cloudReady,
    localReady,
    localAvailable,
    missing,
    defaultProvider,
    sampleProvider,
  }
}

/** Where the guide's sample job stands (per backend profile). */
export type SamplePhase = 'idle' | 'fetching' | 'uploading' | 'starting' | 'running' | 'succeeded' | 'failed'

/** Stored per backend profile so the step stays done (and a running job is picked up again). */
export interface StoredSample {
  jobId: string
  transcriptId: string | null
}

export function parseStoredSample(raw: string | null): StoredSample | null {
  try {
    const v = JSON.parse(raw ?? 'null') as Partial<StoredSample> | null
    if (!v || typeof v.jobId !== 'string' || !v.jobId) return null
    return { jobId: v.jobId, transcriptId: typeof v.transcriptId === 'string' && v.transcriptId ? v.transcriptId : null }
  } catch {
    return null
  }
}
