/**
 * SiYuan document paths, mirrored from the backend (`asr/export/siyuan.py`) so the export dialog
 * can preview what the path template will produce and the Settings page can flag a bad template
 * before saving. The backend stays authoritative: it renders the real path (server-local time).
 */

/** Placeholders allowed in the path template (backend `PATH_FIELDS`). */
export const PATH_FIELDS = ['title', 'date', 'time', 'source', 'provider'] as const
export type PathField = (typeof PATH_FIELDS)[number]
export type PathValues = Partial<Record<PathField, string>>

export const DEFAULT_PATH_TEMPLATE = '/会议记录/{date} {title}'
const MAX_SEGMENT_CHARS = 200
const MAX_PATH_CHARS = 1000
const PLACEHOLDER = /\{([^{}]*)\}/g

/** Line breaks / tabs to spaces, control characters dropped (backend `one_line`). */
function oneLine(text: string): string {
  const joined = text
    .split(/\r\n|[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]/)
    .map((p) => p.trim())
    .filter(Boolean)
    .join(' ')
  // eslint-disable-next-line no-control-regex
  return joined.replace(/\t/g, ' ').replace(/[\x00-\x1f\x7f-\x9f]/g, '')
}

/** One document title: single line, `/` as the full-width `／`, at most 200 characters. */
function titleSegment(value: string): string {
  const text = oneLine(value).replace(/\//g, '／').split(/\s+/).filter(Boolean).join(' ')
  return [...text].slice(0, MAX_SEGMENT_CHARS).join('').trim()
}

/**
 * Fill the placeholders per path segment (values never add levels; empty segments are dropped).
 * Null when nothing is left or the path is too long (the backend would answer 422).
 */
export function renderPathTemplate(template: string, values: PathValues): string | null {
  const segments = template
    .trim()
    .split('/')
    .slice(1)
    .map((seg) =>
      titleSegment(seg.replace(PLACEHOLDER, (_m, name: string) => titleSegment(values[name as PathField] ?? ''))),
    )
    .filter(Boolean)
  if (!segments.length) return null
  const path = `/${segments.join('/')}`
  return path.length > MAX_PATH_CHARS ? null : path
}

export type TemplateProblem =
  | { reason: 'slash' }
  | { reason: 'placeholder'; name: string }
  | { reason: 'braces' }
  | { reason: 'empty' }

/** Why the backend would reject `template` (`validate_path_template`), or null. */
export function pathTemplateProblem(template: string): TemplateProblem | null {
  const value = template.trim()
  if (!value) return null // "" resets to the default
  if (!value.startsWith('/')) return { reason: 'slash' }
  for (const m of value.matchAll(PLACEHOLDER)) {
    const name = m[1]!
    if (!(PATH_FIELDS as readonly string[]).includes(name)) return { reason: 'placeholder', name }
  }
  const rest = value.replace(PLACEHOLDER, '')
  if (rest.includes('{') || rest.includes('}')) return { reason: 'braces' }
  const filled = Object.fromEntries(PATH_FIELDS.map((f) => [f, 'x']))
  return renderPathTemplate(value, filled) ? null : { reason: 'empty' }
}

function pad(n: number): string {
  return String(n).padStart(2, '0')
}

/** File name without directories (`a/b/c.m4a` → `c.m4a`, Windows separators too). */
export function baseName(name: string): string {
  const parts = name.replace(/\\/g, '/').split('/')
  return parts[parts.length - 1] ?? ''
}

/** File name without the last extension (`周会.m4a` → `周会`; `.env` stays `.env`). */
export function stemOf(name: string): string {
  const base = baseName(name)
  const dot = base.lastIndexOf('.')
  return dot > 0 ? base.slice(0, dot) : base
}

/**
 * Placeholder values as the backend derives them (`export_source`): title = source file stem,
 * date / time of the upload (the browser's timezone here; the server uses its own).
 */
export function pathValues(input: {
  sourceName: string | null
  createdAt: string | null
  provider: string
}): PathValues {
  const source = input.sourceName ? baseName(input.sourceName) : ''
  const stem = input.sourceName ? stemOf(input.sourceName) : ''
  const when = input.createdAt ? new Date(input.createdAt) : null
  const valid = when && Number.isFinite(when.getTime())
  return {
    // The backend's fallback file stem "transcript" means "no title".
    title: stem === 'transcript' ? '' : stem,
    source,
    date: valid ? `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())}` : '',
    time: valid ? `${pad(when.getHours())}:${pad(when.getMinutes())}` : '',
    provider: input.provider,
  }
}

/** The export `locale` for a UI locale (`zh-CN` → `zh`). */
export function exportLocaleOf(uiLocale: string): 'zh' | 'en' {
  return uiLocale.toLowerCase().startsWith('en') ? 'en' : 'zh'
}
