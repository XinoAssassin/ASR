/** Level-meter scale: linear peak amplitude 0..1 → dBFS → bar fraction over a 60 dB range. */
export const METER_FLOOR_DB = -60

export function peakToDb(peak: number): number {
  if (!Number.isFinite(peak) || peak <= 0) return -Infinity
  return 20 * Math.log10(Math.min(peak, 1))
}

/** 0..1 bar fill for a linear peak (−60 dBFS and below → 0, 0 dBFS → 1). */
export function meterFraction(peak: number): number {
  const db = peakToDb(peak)
  if (db === -Infinity) return 0
  return Math.min(1, Math.max(0, (db - METER_FLOOR_DB) / -METER_FLOOR_DB))
}

/** "-12 dB", or null below the meter floor. */
export function formatDb(peak: number): string | null {
  const db = peakToDb(peak)
  return db < METER_FLOOR_DB ? null : `${Math.round(db)} dB`
}
