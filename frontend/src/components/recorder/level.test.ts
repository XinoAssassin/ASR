import { describe, expect, it } from 'vitest'

import { formatDb, meterFraction, peakToDb } from './level'

describe('level meter scale', () => {
  it('maps linear peaks onto a 60 dB bar', () => {
    expect(meterFraction(0)).toBe(0)
    expect(meterFraction(1)).toBe(1)
    expect(meterFraction(2)).toBe(1)
    expect(meterFraction(0.001)).toBeCloseTo(0, 5) // -60 dBFS
    expect(meterFraction(0.1)).toBeCloseTo(2 / 3, 5) // -20 dBFS
    expect(meterFraction(Number.NaN)).toBe(0)
  })

  it('formats dB and hides silence', () => {
    expect(peakToDb(0)).toBe(-Infinity)
    expect(formatDb(0.5)).toBe('-6 dB')
    expect(formatDb(0)).toBeNull()
    expect(formatDb(0.0001)).toBeNull()
  })
})
