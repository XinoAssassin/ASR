import { describe, expect, it } from 'vitest'

import { basename, elapsedSeconds, formatBytes, formatClock, formatDuration, formatTimestamp } from './format'

describe('format', () => {
  it('formats durations', () => {
    expect(formatDuration(0)).toBe('0:00')
    expect(formatDuration(65.9)).toBe('1:05')
    expect(formatDuration(3725.4)).toBe('1:02:05')
    expect(formatDuration(null)).toBe('—')
    expect(formatDuration(Number.NaN)).toBe('—')
  })

  it('formats timestamps and clock', () => {
    expect(formatTimestamp(65.37)).toBe('01:05')
    expect(formatTimestamp(3725.4)).toBe('1:02:05')
    expect(formatTimestamp(-1)).toBe('00:00')
    expect(formatClock(65.37)).toBe('1:05.3')
  })

  it('formats bytes in binary units', () => {
    expect(formatBytes(512)).toBe('512 B')
    expect(formatBytes(1536)).toBe('1.50 KB')
    expect(formatBytes(63 * 1024 * 1024)).toBe('63.0 MB')
    expect(formatBytes(1024 ** 3)).toBe('1.00 GB')
  })

  it('computes elapsed time', () => {
    expect(elapsedSeconds('2026-01-01T00:00:00Z', '2026-01-01T00:01:30Z')).toBe(90)
    expect(elapsedSeconds(null, null)).toBeNull()
    expect(elapsedSeconds('2026-01-01T00:00:00Z', null, Date.parse('2026-01-01T00:00:10Z'))).toBe(10)
  })

  it('extracts basenames', () => {
    expect(basename('/data/uploads/a/会议.m4a')).toBe('会议.m4a')
    expect(basename('C:\\rec\\x.wav')).toBe('x.wav')
    expect(basename('https://h/x/y.mp3?sig=1')).toBe('y.mp3')
  })
})
