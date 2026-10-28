/**
 * Audio heuristics mirrored from the backend (D8) for UI hints only; the server decides.
 */
import type { ChannelsOption, ProbeInfo } from '@/api/endpoints'

/** Accepted upload extensions (asr.audio.formats.ACCEPTED_EXTENSIONS). */
export const ACCEPTED_EXTENSIONS = [
  'm4a',
  'mp3',
  'wav',
  'flac',
  'aac',
  'caf',
  'ogg',
  'opus',
  'webm',
  'mp4',
  'm4v',
  'mov',
  'mkv',
] as const

/** Value for `<input accept>`. */
export const ACCEPT_ATTRIBUTE = ACCEPTED_EXTENSIONS.map((ext) => `.${ext}`).join(',')

export function fileExtension(name: string): string {
  const dot = name.lastIndexOf('.')
  return dot >= 0 ? name.slice(dot + 1).toLowerCase() : ''
}

export function isAcceptedFile(name: string): boolean {
  return (ACCEPTED_EXTENSIONS as readonly string[]).includes(fileExtension(name))
}

/**
 * Size of 16 kHz FLAC speech per hour and channel (≈ 63 MB; docs/research/recording-sources.md).
 * The only place this constant lives.
 */
export const FLAC_16K_MB_PER_HOUR_PER_CHANNEL = 63

/** DashScope temp-upload cap used when no connection test has reported one. */
export const DEFAULT_CLOUD_MAX_FILE_SIZE_MB = 1024

const MB = 1024 * 1024

export function estimateFlacBytes(durationSeconds: number, channels: number): number {
  return Math.round(
    (durationSeconds / 3600) * Math.max(1, channels) * FLAC_16K_MB_PER_HOUR_PER_CHANNEL * MB,
  )
}

/** Effective channel handling after the user's override (ResolvedChannels.mode). */
export type ResolvedMode = 'mono' | 'downmix' | 'per_channel' | 'select'

export function resolveChannelMode(probe: ProbeInfo, option: ChannelsOption): ResolvedMode {
  if (probe.channels <= 1) return 'mono'
  switch (option) {
    case 'auto':
      return probe.channel_policy
    case 'downmix':
      return 'downmix'
    case 'per_channel':
      return 'per_channel'
    case 'left':
    case 'right':
      return 'select'
  }
}

export interface CloudPrepEstimate {
  /** True when the backend will transcode to 16 kHz FLAC before uploading to DashScope. */
  transcode: boolean
  reason: 'size' | 'downmix' | null
  /** Estimated FLAC size; null when the duration is unknown or no transcode happens. */
  estimatedBytes: number | null
  /** Channels in the transcoded file. */
  channels: number
}

/**
 * D8: transcode when the file exceeds the temp-upload cap, or when the resolved policy is a
 * downmix of a multi-channel file (sent as mono FLAC). per_channel / select keep the channels.
 */
export function estimateCloudPrep(
  probe: ProbeInfo,
  option: ChannelsOption,
  maxFileSizeMb: number = DEFAULT_CLOUD_MAX_FILE_SIZE_MB,
): CloudPrepEstimate {
  const mode = resolveChannelMode(probe, option)
  const tooLarge = probe.size > maxFileSizeMb * MB
  const downmix = mode === 'downmix' && probe.channels > 1
  const transcode = tooLarge || downmix
  const channels = mode === 'per_channel' || mode === 'select' ? probe.channels : 1
  return {
    transcode,
    reason: tooLarge ? 'size' : downmix ? 'downmix' : null,
    estimatedBytes:
      transcode && probe.duration != null && probe.duration > 0
        ? estimateFlacBytes(probe.duration, channels)
        : null,
    channels,
  }
}
