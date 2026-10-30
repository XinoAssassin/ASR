/** Transcript search: case-insensitive literal matches across segments, in display order. */
import type { Segment, Term } from '@/api/endpoints'

import { literalPattern } from './highlight'

export interface Match {
  segmentId: string
  start: number
  end: number
}

export function findMatches(segments: Segment[], query: string): Match[] {
  const q = query.trim()
  if (!q) return []
  const pattern = literalPattern(q)
  const out: Match[] = []
  for (const seg of segments) {
    for (const m of seg.text.matchAll(pattern)) {
      if (!m[0]) break
      out.push({ segmentId: seg.id, start: m.index, end: m.index + m[0].length })
    }
  }
  return out
}

/** Term list with `alias` added to `target` (created when missing); aliases stay unique. */
export function withAlias(terms: Term[], target: string, alias: string): Term[] {
  const key = target.trim().toLowerCase()
  const word = alias.trim()
  if (!key || !word || word.toLowerCase() === key) return terms
  let found = false
  const out = terms.map((t) => {
    if (t.term.toLowerCase() !== key) return t
    found = true
    const aliases = t.aliases ?? []
    return aliases.includes(word) ? t : { ...t, aliases: [...aliases, word] }
  })
  if (!found) out.push({ term: target.trim(), weight: 3, aliases: [word], blacklist: [] })
  return out
}
