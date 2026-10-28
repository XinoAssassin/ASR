/**
 * Extract the download filename from a Content-Disposition header (RFC 6266). `filename*`
 * (RFC 5987, e.g. `UTF-8''%E4%BC%9A%E8%AE%AE.srt`) wins over the plain `filename` parameter.
 * Path separators are stripped so the result is always a bare file name.
 */
export function parseContentDispositionFilename(header: string | null | undefined): string | null {
  if (!header) return null
  const params = splitParams(header)
  let extended: string | null = null
  let plain: string | null = null
  for (const raw of params) {
    const eq = raw.indexOf('=')
    if (eq < 0) continue
    const name = raw.slice(0, eq).trim().toLowerCase()
    const value = raw.slice(eq + 1).trim()
    if (name === 'filename*') extended = decodeExtValue(value)
    else if (name === 'filename') plain = unquote(value)
  }
  const result = extended ?? plain
  return result ? sanitize(result) : null
}

/** Split on `;` outside double quotes. */
function splitParams(header: string): string[] {
  const out: string[] = []
  let current = ''
  let quoted = false
  for (let i = 0; i < header.length; i++) {
    const ch = header[i]!
    if (ch === '\\' && quoted && i + 1 < header.length) {
      current += ch + header[i + 1]
      i++
      continue
    }
    if (ch === '"') quoted = !quoted
    if (ch === ';' && !quoted) {
      out.push(current)
      current = ''
    } else {
      current += ch
    }
  }
  out.push(current)
  return out
}

function unquote(value: string): string {
  if (value.length >= 2 && value.startsWith('"') && value.endsWith('"')) {
    return value.slice(1, -1).replace(/\\(.)/g, '$1')
  }
  return value
}

function decodeExtValue(value: string): string | null {
  const v = unquote(value)
  const match = /^([^']*)'[^']*'(.*)$/.exec(v)
  if (!match) return null
  const charset = match[1]!.toLowerCase()
  const encoded = match[2]!
  try {
    if (charset === 'utf-8' || charset === '') return decodeURIComponent(encoded)
    // ISO-8859-1: percent-decode bytes one-to-one.
    return encoded.replace(/%([0-9a-f]{2})/gi, (_, hex: string) =>
      String.fromCharCode(parseInt(hex, 16)),
    )
  } catch {
    return null
  }
}

function sanitize(name: string): string | null {
  const base = name.split(/[\\/]/).pop()?.trim() ?? ''
  return base && base !== '.' && base !== '..' ? base : null
}
