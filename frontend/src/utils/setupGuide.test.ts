import { describe, expect, it } from 'vitest'

import type { LocalModelOut, SettingsOut } from '@/api/endpoints'

import { missingLocalModels, parseStoredSample, recognitionState } from './setupGuide'

const model = (id: string, kind: LocalModelOut['kind'], downloaded = false): LocalModelOut => ({
  id,
  kind,
  name: id,
  downloaded,
  status: downloaded ? 'downloaded' : 'missing',
  size_mb: 100,
  error: null,
  has_mirror: false,
})

const registry = (downloaded: string[] = []): LocalModelOut[] =>
  [
    ['qwen3-asr-0.6b', 'asr'],
    ['qwen3-asr-1.7b', 'asr'],
    ['seaco-paraformer', 'asr'],
    ['faster-whisper-small', 'asr'],
    ['qwen3-forced-aligner', 'aligner'],
    ['campplus', 'diarization'],
    ['fsmn-vad', 'vad'],
  ].map(([id, kind]) => model(id!, kind as LocalModelOut['kind'], downloaded.includes(id!)))

function server(patch: { key?: boolean; provider?: 'local' | 'cloud'; asr?: string | null; diarize?: string | null } = {}): SettingsOut {
  return {
    provider_default: patch.provider ?? 'cloud',
    cloud: { api_key_set: !!patch.key },
    local: { asr_model: patch.asr ?? null, diarize_model: patch.diarize ?? null, device: 'cpu', model_cache_dir: '/m' },
  } as unknown as SettingsOut
}

describe('missingLocalModels', () => {
  it('needs VAD, an auto Qwen3 size, the aligner and CAM++', () => {
    expect(missingLocalModels(registry(), null, null).map((m) => m.id)).toEqual([
      'fsmn-vad',
      'qwen3-asr-0.6b',
      'qwen3-forced-aligner',
      'campplus',
    ])
  })

  it('accepts either Qwen3 size for auto', () => {
    const all = ['fsmn-vad', 'qwen3-asr-1.7b', 'qwen3-forced-aligner', 'campplus']
    expect(missingLocalModels(registry(all), null, null)).toEqual([])
  })

  it('uses the configured ASR model; Paraformer / Whisper need no aligner', () => {
    expect(missingLocalModels(registry(['fsmn-vad', 'campplus', 'qwen3-asr-0.6b']), 'seaco-paraformer', null).map((m) => m.id)).toEqual([
      'seaco-paraformer',
    ])
    expect(missingLocalModels(registry(['fsmn-vad', 'campplus', 'faster-whisper-small']), 'faster-whisper-small', null)).toEqual([])
  })

  it('ignores roles the backend does not list', () => {
    expect(missingLocalModels([model('qwen3-asr-0.6b', 'asr', true)], null, null)).toEqual([])
  })
})

describe('recognitionState', () => {
  const ready = ['fsmn-vad', 'qwen3-asr-0.6b', 'qwen3-forced-aligner', 'campplus']

  it('uses the default provider when it is ready', () => {
    const s = recognitionState(server({ key: true, provider: 'cloud' }), registry(ready), true)
    expect(s).toMatchObject({ loaded: true, cloudReady: true, localReady: true, sampleProvider: 'cloud' })
  })

  it('falls back to the provider that is ready', () => {
    expect(recognitionState(server({ provider: 'cloud' }), registry(ready), true).sampleProvider).toBe('local')
    expect(recognitionState(server({ key: true, provider: 'local' }), registry(), true).sampleProvider).toBe('cloud')
  })

  it('reports nothing usable and what is missing', () => {
    const s = recognitionState(server({ provider: 'local' }), registry(['fsmn-vad']), true)
    expect(s.sampleProvider).toBeNull()
    expect(s.missing.map((m) => m.id)).toEqual(['qwen3-asr-0.6b', 'qwen3-forced-aligner', 'campplus'])
  })

  it('handles a backend without local models and an unloaded state', () => {
    expect(recognitionState(server({ key: true }), [], true)).toMatchObject({ localAvailable: false, localReady: false })
    expect(recognitionState(null, [], false)).toMatchObject({ loaded: false, sampleProvider: null })
  })
})

describe('parseStoredSample', () => {
  it('reads valid entries only', () => {
    expect(parseStoredSample('{"jobId":"j1","transcriptId":"t1"}')).toEqual({ jobId: 'j1', transcriptId: 't1' })
    expect(parseStoredSample('{"jobId":"j1"}')).toEqual({ jobId: 'j1', transcriptId: null })
    expect(parseStoredSample('{"jobId":""}')).toBeNull()
    expect(parseStoredSample('nope')).toBeNull()
    expect(parseStoredSample(null)).toBeNull()
  })
})
