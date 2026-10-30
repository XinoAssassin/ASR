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

/** RegExp special characters (escaping only these keeps the pattern valid with the `u` flag). */
const REGEX_SPECIAL = /[.*+?^${}()|[\]\\]/g

/**
 * Case-insensitive literal matcher, like the backend's `re.compile(re.escape(find), re.I)`.
 * Matching on the original string keeps indices right even where lower-casing changes the length
 * (`İ` → `i̇`), which mapping `toLowerCase()` indices back onto the original did not.
 */
export function literalPattern(needle: string, caseInsensitive = true): RegExp {
  return new RegExp(needle.replace(REGEX_SPECIAL, '\\$&'), caseInsensitive ? 'giu' : 'gu')
}

/** Non-overlapping matches of `needle` in `text` as [start, end) UTF-16 ranges. */
export function occurrences(
  text: string,
  needle: string,
  caseInsensitive = true,
): Array<{ start: number; end: number }> {
  if (!needle) return []
  const out: Array<{ start: number; end: number }> = []
  for (const m of text.matchAll(literalPattern(needle, caseInsensitive))) {
    if (!m[0]) break
    out.push({ start: m.index, end: m.index + m[0].length })
  }
  return out
}

export function termMarks(text: string, terms: string[]): Mark[] {
  return terms.flatMap((term) => occurrences(text, term).map((r) => ({ ...r, kind: 'term' as const })))
}

export function searchMarks(text: string, query: string): Mark[] {
  return occurrences(text, query).map((r) => ({ ...r, kind: 'search' as const }))
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

export interface PieceInputs {
  text: string
  /** Job terms (highlighted). */
  terms: string[]
  /** Corrections of this segment. */
  corrections: Correction[]
  /** Trimmed search query ('' = no search). */
  query: string
  /** The current search match, when it is in this segment. */
  current: { start: number; end: number } | null
}

interface MemoEntry {
  text: string
  termsKey: string
  correctionsKey: string
  baseMarks: Mark[]
  base: Piece[]
  searchKey: string
  full: Piece[]
}

function correctionsKey(corrections: Correction[]): string {
  return corrections
    .map((c) => [c.id, c.status, c.before, c.after, c.offset ?? ''].join('\u0001'))
    .join('\u0002')
}

/**
 * Per-segment cache of `pieces()`: returns the same array as long as the segment's text, the
 * terms, its corrections and the search state are unchanged, so a re-render of the transcript
 * (playback moving the active segment, a keystroke in another segment) neither rescans the text
 * nor re-renders the rows whose highlights did not change. Search marks are layered on top of the
 * cached term / correction marks; a segment without search hits keeps its base pieces.
 */
export class PiecesMemo {
  private readonly entries = new Map<string, MemoEntry>()
  private lastTerms: string[] | null = null
  private lastTermsKey = ''

  get(segmentId: string, inputs: PieceInputs): Piece[] {
    const { text, terms, corrections, query, current } = inputs
    if (terms !== this.lastTerms) {
      this.lastTerms = terms
      this.lastTermsKey = terms.join('\u0000')
    }
    const termsKey = this.lastTermsKey
    const corrKey = correctionsKey(corrections)
    let entry = this.entries.get(segmentId)
    if (
      !entry ||
      entry.text !== text ||
      entry.termsKey !== termsKey ||
      entry.correctionsKey !== corrKey
    ) {
      const baseMarks = [...termMarks(text, terms), ...correctionMarks(text, corrections)]
      const base = pieces(text, baseMarks)
      entry = { text, termsKey, correctionsKey: corrKey, baseMarks, base, searchKey: '', full: base }
      this.entries.set(segmentId, entry)
    }
    const searchKey = query ? `${query}\u0000${current ? `${current.start}:${current.end}` : ''}` : ''
    if (!searchKey) return entry.base
    if (entry.searchKey !== searchKey) {
      const marks = searchMarks(text, query)
      if (current) marks.push({ start: current.start, end: current.end, kind: 'current' })
      entry.searchKey = searchKey
      entry.full = marks.length ? pieces(text, [...entry.baseMarks, ...marks]) : entry.base
    }
    return entry.full
  }

  /** Forget segments that no longer exist (merged, another transcript). */
  retain(ids: Iterable<string>): void {
    const keep = new Set(ids)
    for (const id of this.entries.keys()) if (!keep.has(id)) this.entries.delete(id)
  }

  clear(): void {
    this.entries.clear()
  }

  get size(): number {
    return this.entries.size
  }
}
