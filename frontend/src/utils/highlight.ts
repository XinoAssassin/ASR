/**
 * Proofreading highlights: split a segment's text into plain pieces carrying mark kinds, so the
 * template renders spans (never v-html). Marks may overlap; pieces are cut at every boundary.
 */
import type { Correction } from '@/api/endpoints'

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

/** Applied / accepted corrections mark the new text; suggestions mark the text they would change. */
export function correctionMarks(text: string, corrections: Correction[]): Mark[] {
  const marks: Mark[] = []
  for (const c of corrections) {
    if (c.status === 'rejected') continue
    const suggestion = c.status === 'suggested'
    const needle = suggestion ? c.before : c.after
    if (!needle) continue
    const offset = c.offset ?? null
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
