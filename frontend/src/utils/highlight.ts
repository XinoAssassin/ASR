/**
 * Proofreading highlights: split a segment's text into plain pieces carrying mark kinds, so the
 * template renders spans (never v-html). Marks may overlap; pieces are cut at every boundary.
 * All mark positions are UTF-16 indices; `Correction.offset` from the server counts code points
 * and is converted here.
 */
import type { Correction } from '@/api/endpoints'

import { codePointToUtf16 } from './text'

export type MarkKind = 'term' | 'search' | 'current' | 'correction' | 'suggestion'

export interface Mark {
  start: number
  end: number
  kind: MarkKind
  /** Correction id for correction / suggestion marks. */
  ref?: string
}

export interface Piece {
  text: string
  kinds: MarkKind[]
  refs: string[]
}

function occurrences(text: string, needle: string, caseInsensitive: boolean): number[] {
  if (!needle) return []
  const hay = caseInsensitive ? text.toLowerCase() : text
  const pin = caseInsensitive ? needle.toLowerCase() : needle
  const out: number[] = []
  let i = hay.indexOf(pin)
  while (i >= 0) {
    out.push(i)
    i = hay.indexOf(pin, i + pin.length)
  }
  return out
}

export function termMarks(text: string, terms: string[]): Mark[] {
  return terms.flatMap((term) =>
    occurrences(text, term, true).map((start) => ({ start, end: start + term.length, kind: 'term' as const })),
  )
}

export function searchMarks(text: string, query: string): Mark[] {
  return occurrences(text, query, true).map((start) => ({
    start,
    end: start + query.length,
    kind: 'search' as const,
  }))
}

/** A correction's server offset (code points) as a UTF-16 index into `text`; null when absent. */
function correctionOffset(text: string, c: Correction): number | null {
  return c.offset == null ? null : codePointToUtf16(text, c.offset)
}

/** Applied / accepted corrections mark the new text; suggestions mark the text they would change. */
export function correctionMarks(text: string, corrections: Correction[]): Mark[] {
  const marks: Mark[] = []
  for (const c of corrections) {
    if (c.status === 'rejected') continue
    const suggestion = c.status === 'suggested'
    const needle = suggestion ? c.before : c.after
    if (!needle) continue
    const offset = correctionOffset(text, c)
    const start =
      offset !== null && text.slice(offset, offset + needle.length) === needle
        ? offset
        : text.indexOf(needle)
    if (start < 0) continue
    marks.push({
      start,
      end: start + needle.length,
      kind: suggestion ? 'suggestion' : 'correction',
      ref: c.id,
    })
  }
  return marks
}

/**
 * Text around a correction in its segment, for reviewing it out of line: `hit` is what the segment
 * currently shows at that place (the new text if applied / accepted, the old one otherwise; empty
 * for a pending insertion). Null when the place cannot be found any more.
 */
export function correctionContext(
  text: string,
  c: Correction,
  width = 10,
): { left: string; hit: string; right: string } | null {
  const current = c.status === 'applied' || c.status === 'accepted' ? c.after : c.before
  const offset = correctionOffset(text, c)
  let start: number
  if (offset !== null && offset <= text.length && text.slice(offset, offset + current.length) === current) {
    start = offset
  } else if (current) {
    start = text.indexOf(current)
    if (start < 0) return null
  } else {
    return null
  }
  const end = start + current.length
  const left = text.slice(Math.max(0, start - width), start)
  const right = text.slice(end, end + width)
  return {
    left: (start > width ? '…' : '') + left,
    hit: current,
    right: right + (end + width < text.length ? '…' : ''),
  }
}

export function pieces(text: string, marks: Mark[]): Piece[] {
  const cuts = new Set<number>([0, text.length])
  for (const m of marks) {
    cuts.add(Math.max(0, Math.min(text.length, m.start)))
    cuts.add(Math.max(0, Math.min(text.length, m.end)))
  }
  const points = [...cuts].sort((a, b) => a - b)
  const out: Piece[] = []
  for (let i = 0; i < points.length - 1; i++) {
    const a = points[i]!
    const b = points[i + 1]!
    if (b <= a) continue
    const covering = marks.filter((m) => m.start <= a && m.end >= b)
    const piece: Piece = {
      text: text.slice(a, b),
      kinds: [...new Set(covering.map((m) => m.kind))],
      refs: [...new Set(covering.flatMap((m) => (m.ref ? [m.ref] : [])))],
    }
    const prev = out[out.length - 1]
    if (prev && prev.kinds.join() === piece.kinds.join() && prev.refs.join() === piece.refs.join()) {
      prev.text += piece.text
    } else {
      out.push(piece)
    }
  }
  return out
}
