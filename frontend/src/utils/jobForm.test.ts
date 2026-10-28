import { describe, expect, it } from 'vitest'

import { buildJobCreate, defaultJobForm } from './jobForm'

describe('buildJobCreate', () => {
  it('maps the form to JobCreate', () => {
    const form = {
      ...defaultJobForm(),
      speakerMode: 'fixed' as const,
      numSpeakers: 4,
      termSetIds: ['ts1'],
      context: '  产品评审会  ',
      keepDialect: true,
    }
    const body = buildJobCreate(
      form,
      { uploadId: 'u1', channels: 'per_channel', streamIndex: 1 },
      [{ term: 'Kubernetes', weight: 3 }],
    )
    expect(body).toEqual({
      upload_id: 'u1',
      provider: 'cloud',
      term_set_ids: ['ts1'],
      extra_terms: [{ term: 'Kubernetes', weight: 3 }],
      options: {
        provider: 'cloud',
        language_hints: ['zh', 'en'],
        num_speakers: 4,
        context: '产品评审会',
        preprocess: { channels: 'per_channel', stream_index: 1, peak_gain: true },
        cloud: { keep_dialect: true },
      },
    })
  })

  it('uses null for auto speakers and empty context, clamps the count', () => {
    const auto = buildJobCreate(defaultJobForm(), { uploadId: 'u', channels: 'auto', streamIndex: 0 }, [])
    expect(auto.options?.num_speakers).toBeNull()
    expect(auto.options?.context).toBeNull()
    const clamped = buildJobCreate(
      { ...defaultJobForm(), speakerMode: 'fixed', numSpeakers: 99 },
      { uploadId: 'u', channels: 'auto', streamIndex: 0 },
      [],
    )
    expect(clamped.options?.num_speakers).toBe(20)
  })
})
