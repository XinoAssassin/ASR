/** Formatting helpers shared by views. All times are seconds (float), per the data model. */

/** 3725.4 → "1:02:05"; 65 → "1:05"; null → "—". */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return '—'
  const total = Math.floor(seconds)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  const ss = String(s).padStart(2, '0')
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`
}

/** Transcript timestamp: 65.37 → "01:05"; 3725.4 → "1:02:05". */
export function formatTimestamp(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  const mmss = `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
  return h > 0 ? `${h}:${mmss}` : mmss
}

/** Player clock with tenths: 65.37 → "1:05.3". */
export function formatClock(seconds: number): string {
  const safe = Number.isFinite(seconds) && seconds > 0 ? seconds : 0
  const tenths = Math.floor((safe * 10) % 10)
  return `${formatDuration(safe)}.${tenths}`
}

const UNITS = ['B', 'KB', 'MB', 'GB', 'TB'] as const

/** Binary units (1 KB = 1024 B), matching how the backend's `*_mb` limits are interpreted. */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null || !Number.isFinite(bytes) || bytes < 0) return '—'
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024
    unit++
  }
  const digits = unit === 0 || value >= 100 ? 0 : value >= 10 ? 1 : 2
  return `${value.toFixed(digits)} ${UNITS[unit]}`
}

/** Seconds between two ISO timestamps (end defaults to `now`); null if start is missing. */
export function elapsedSeconds(
  startIso: string | null | undefined,
  endIso: string | null | undefined,
  now: number = Date.now(),
): number | null {
  if (!startIso) return null
  const start = Date.parse(startIso)
  if (Number.isNaN(start)) return null
  const end = endIso ? Date.parse(endIso) : now
  if (Number.isNaN(end)) return null
  return Math.max(0, (end - start) / 1000)
}

/** Locale date-time for an ISO timestamp. */
export function formatDateTime(iso: string | null | undefined, locale?: string): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleString(locale, {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

/** Last path component of a POSIX / Windows path or URL. */
export function basename(path: string | null | undefined): string {
  if (!path) return ''
  const clean = path.split(/[?#]/)[0]!.replace(/[\\/]+$/, '')
  return clean.split(/[\\/]/).pop() ?? clean
}

/**
 * A percentage clamped to 0..100 and rounded to at most `digits` decimals (36.60946… → 36.61);
 * null for an unknown / non-finite value.
 */
export function roundPercent(value: number | null | undefined, digits = 2): number | null {
  if (value == null || !Number.isFinite(value)) return null
  const factor = 10 ** digits
  return Math.round(Math.min(100, Math.max(0, value)) * factor) / factor
}

/** 36.60946… → "36.61%", 64 → "64%" (no trailing zeros); "" when unknown. */
export function formatPercent(value: number | null | undefined, digits = 2): string {
  const rounded = roundPercent(value, digits)
  return rounded == null ? '' : `${rounded}%`
}
