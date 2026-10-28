/**
 * Ad-hoc term list parsing for the job form: one term per line, optionally `term,weight`.
 * Weight is 1..5 or 50 (boosted), default 3 (asr.providers.base.Term). Full-width commas work too.
 */
import type { Term } from '@/api/endpoints'

export const DEFAULT_TERM_WEIGHT = 3
export const MAX_TERM_LENGTH = 100

export interface TermParseError {
  line: number
  text: string
  reason: 'weight' | 'length'
}

export interface TermParseResult {
  terms: Term[]
  errors: TermParseError[]
}

export function isValidWeight(weight: number): boolean {
  return Number.isInteger(weight) && ((weight >= 1 && weight <= 5) || weight === 50)
}

export function parseTerms(text: string): TermParseResult {
  const terms: Term[] = []
  const errors: TermParseError[] = []
  const seen = new Set<string>()
  const lines = text.split(/\r?\n/)
  lines.forEach((raw, index) => {
    const line = raw.trim()
    if (!line || line.startsWith('#')) return
    let term = line
    let weight = DEFAULT_TERM_WEIGHT
    const match = /^(.*?)[,，]\s*([^,，]*)$/.exec(line)
    if (match) {
      term = match[1]!.trim()
      const w = match[2]!.trim()
      if (w) {
        const parsed = Number(w)
        if (!isValidWeight(parsed)) {
          errors.push({ line: index + 1, text: raw, reason: 'weight' })
          return
        }
        weight = parsed
      }
    }
    if (!term) return
    if (term.length > MAX_TERM_LENGTH) {
      errors.push({ line: index + 1, text: raw, reason: 'length' })
      return
    }
    if (seen.has(term)) return
    seen.add(term)
    terms.push({ term, weight })
  })
  return { terms, errors }
}
