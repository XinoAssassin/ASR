/**
 * Ad-hoc term list parsing for the job form: one term per line, optionally `term,weight`.
 * The term part may use the asr-hotword syntax `Target | alias | alias ~~~ black | black`
 * (aliases = known mis-hearings fixed by post-correction; blacklist = neighbours that block it).
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
    const [names = '', black = ''] = term.split('~~~')
    const [target = '', ...aliases] = names.split('|').map((s) => s.trim())
    const blacklist = black ? black.split('|').map((s) => s.trim()) : []
    term = target
    if (!term) return
    if ([term, ...aliases, ...blacklist].some((w) => w.length > MAX_TERM_LENGTH)) {
      errors.push({ line: index + 1, text: raw, reason: 'length' })
      return
    }
    if (seen.has(term)) return
    seen.add(term)
    terms.push({
      term,
      weight,
      aliases: aliases.filter(Boolean),
      blacklist: blacklist.filter(Boolean),
    })
  })
  return { terms, errors }
}
