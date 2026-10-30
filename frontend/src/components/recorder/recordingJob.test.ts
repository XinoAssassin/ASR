import { describe, expect, it } from 'vitest'

import { parseTerms } from '@/utils/terms'

import { defaultRecordingJob, formatTerms, fromJobConfig, toJobConfig } from './recordingJob'

describe('recording job config', () => {
  it('defaults to a per_channel job', () => {
    const cfg = toJobConfig(defaultRecordingJob('local'))
    expect(cfg.provider).toBe('local')
    expect(cfg.termSetIds).toEqual([])
    expect(cfg.extraTerms).toEqual([])
    expect(cfg.jobOptions).toMatchObject({
      provider: 'local',
      language_hints: ['zh', 'en'],
      num_speakers: null,
      preprocess: { channels: 'per_channel', peak_gain: true },
    })
  })

  it('upload only → null jobOptions and no terms', () => {
    const settings = defaultRecordingJob()
    settings.createJob = false
    settings.form.termSetIds = ['ts1']
    expect(toJobConfig(settings)).toEqual({
      jobOptions: null,
      provider: 'cloud',
      termSetIds: [],
      extraTerms: [],
    })
  })

  it('sends term sets and parsed extra terms (bad lines dropped)', () => {
    const settings = defaultRecordingJob()
    settings.form.termSetIds = ['ts1', 'ts2']
    settings.form.extraTerms = 'Kubernetes | 酷伯内特斯\nPyTorch,50\nbad,9'
    const cfg = toJobConfig(settings)
    expect(cfg.termSetIds).toEqual(['ts1', 'ts2'])
    expect(cfg.extraTerms).toEqual([
      { term: 'Kubernetes', weight: 3, aliases: ['酷伯内特斯'], blacklist: [] },
      { term: 'PyTorch', weight: 50, aliases: [], blacklist: [] },
    ])
  })

  it('round-trips through BackendConfig', () => {
    const settings = defaultRecordingJob('cloud')
    settings.channels = 'downmix'
    Object.assign(settings.form, {
      speakerMode: 'fixed',
      numSpeakers: 3,
      context: '周会',
      keepDialect: true,
      peakGain: false,
      termCorrection: false,
      repetitionFilter: false,
      languageHints: ['zh'],
      termSetIds: ['ts1'],
      extraTerms: 'Kubernetes | 酷伯内特斯 ~~~ 酷伯\nPyTorch,50',
    })
    expect(fromJobConfig(toJobConfig(settings))).toEqual(settings)
  })

  it('round-trips the hardware of a local job', () => {
    const settings = defaultRecordingJob('local')
    settings.form.device = 'rocm'
    const config = toJobConfig(settings)
    expect(config.jobOptions?.local).toEqual({ device: 'rocm' })
    expect(fromJobConfig(config).form.device).toBe('rocm')
  })

  it('formatTerms is the inverse of parseTerms', () => {
    const terms = [
      { term: 'A,B', weight: 3, aliases: [], blacklist: [] },
      { term: 'GPU', weight: 5, aliases: ['G P U'], blacklist: ['GPS'] },
    ]
    expect(parseTerms(formatTerms(terms)).terms).toEqual(terms)
  })

  it('reads a missing config as "no job" with the fallback provider', () => {
    const s = fromJobConfig({ jobOptions: null, provider: null }, 'local')
    expect(s.createJob).toBe(false)
    expect(s.form.provider).toBe('local')
    expect(s.channels).toBe('per_channel')
  })
})
