import { describe, expect, it } from 'vitest'

import type { ProbeInfo } from '@/api/endpoints'

import {
  estimateCloudPrep,
  estimateFlacBytes,
  FLAC_16K_MB_PER_HOUR_PER_CHANNEL,
  isAcceptedFile,
  resolveChannelMode,
} from './audio'

const MB = 1024 * 1024

function probe(overrides: Partial<ProbeInfo> = {}): ProbeInfo {
  return {
    container: 'mov,mp4,m4a',
    codec: 'aac',
    duration: 3600,
    sample_rate: 48000,
    channels: 1,
    bit_rate: 64000,
    stream_index: 0,
    audio_streams: 1,
    size: 29 * MB,
    channel_policy: 'mono',
    channel_correlation: null,
    ...overrides,
  }
}

describe('audio helpers', () => {
  it('accepts the documented extensions only', () => {
    expect(isAcceptedFile('a.M4A')).toBe(true)
    expect(isAcceptedFile('meeting.mkv')).toBe(true)
    expect(isAcceptedFile('notes.txt')).toBe(false)
    expect(isAcceptedFile('noext')).toBe(false)
  })

  it('estimates FLAC size per hour and channel', () => {
    expect(estimateFlacBytes(3600, 1)).toBe(FLAC_16K_MB_PER_HOUR_PER_CHANNEL * MB)
    expect(estimateFlacBytes(1800, 2)).toBe(FLAC_16K_MB_PER_HOUR_PER_CHANNEL * MB)
  })

  it('resolves the channel mode', () => {
    expect(resolveChannelMode(probe(), 'per_channel')).toBe('mono')
    const stereo = probe({ channels: 2, channel_policy: 'downmix', channel_correlation: 0.93 })
    expect(resolveChannelMode(stereo, 'auto')).toBe('downmix')
    expect(resolveChannelMode(stereo, 'left')).toBe('select')
    expect(resolveChannelMode(stereo, 'per_channel')).toBe('per_channel')
  })

  it('does not transcode a small mono file', () => {
    expect(estimateCloudPrep(probe(), 'auto')).toMatchObject({ transcode: false, reason: null })
  })

  it('transcodes large files and correlated stereo', () => {
    const big = probe({ size: 2048 * MB, duration: 7200 })
    expect(estimateCloudPrep(big, 'auto')).toMatchObject({
      transcode: true,
      reason: 'size',
      estimatedBytes: 2 * FLAC_16K_MB_PER_HOUR_PER_CHANNEL * MB,
    })
    expect(estimateCloudPrep(big, 'auto', 4096).transcode).toBe(false)

    const stereo = probe({ channels: 2, channel_policy: 'downmix' })
    expect(estimateCloudPrep(stereo, 'auto')).toMatchObject({ transcode: true, reason: 'downmix', channels: 1 })
    expect(estimateCloudPrep(stereo, 'per_channel').transcode).toBe(false)
    const bigPerChannel = estimateCloudPrep({ ...stereo, size: 2048 * MB }, 'per_channel')
    expect(bigPerChannel.channels).toBe(2)
  })

  it('omits the estimate when the duration is unknown', () => {
    const stereo = probe({ channels: 2, channel_policy: 'downmix', duration: null })
    expect(estimateCloudPrep(stereo, 'auto').estimatedBytes).toBeNull()
  })
})
