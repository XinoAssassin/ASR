/** Transcript search: case-insensitive literal matches across segments, in display order. */
import type { Segment, Term } from '@/api/endpoints'

export interface Match {
  segmentId: string
  start: number
  end: number
}

export function findMatches(segments: Segment[], query: string): Match[] {
  const q = query.trim().toLowerCase()
  if (!q) return []
  const out: Match[] = []
  for (const seg of segments) {
    const text = seg.text.toLowerCase()
    let i = text.indexOf(q)
    while (i >= 0) {
      out.push({ segmentId: seg.id, start: i, end: i + q.length })
      i = text.indexOf(q, i + q.length)
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
