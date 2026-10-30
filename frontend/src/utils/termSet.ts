/**
 * Term set editing (Term sets page): the editable draft, validation matching the backend
 * (`asr.providers.base.Term`, `server/routes/term_sets.py`) and the txt export in the asr-hotword
 * line syntax that `POST /api/term-sets/import` reads back:
 *
 *   Target | alias | alias ~~~ black | black, weight, note
 *
 * Tabs replace the commas when the term part itself contains a comma (`parse_terms` splits on
 * tabs first).
 */
import type { Term } from '@/api/endpoints'

import { DEFAULT_TERM_WEIGHT, MAX_TERM_LENGTH, isValidWeight } from './terms'

export const BOOSTED_WEIGHT = 50
export const WEIGHTS = [1, 2, 3, 4, 5, BOOSTED_WEIGHT] as const
export const MAX_NAME_LENGTH = 100
export const MAX_NOTE_LENGTH = 500
export const MAX_WORDS = 20
export const MAX_TERMS_PER_SET = 1000
/** Cloud vocabulary limits (DashScope): boosted terms beyond this are sent with weight 5. */
export const MAX_BOOSTED_TERMS = 50

export interface TermRow {
  /** Stable row key for the table (not sent). */
  key: number
  term: string
  weight: number
  note: string
  aliases: string[]
  blacklist: string[]
}

let nextKey = 1

export function newRow(init: Partial<Omit<TermRow, 'key'>> = {}): TermRow {
  return {
    key: nextKey++,
    term: init.term ?? '',
    weight: init.weight ?? DEFAULT_TERM_WEIGHT,
    note: init.note ?? '',
    aliases: [...(init.aliases ?? [])],
    blacklist: [...(init.blacklist ?? [])],
  }
}

export function rowsFromTerms(terms: Term[]): TermRow[] {
  return terms.map((t) =>
    newRow({
      term: t.term,
      weight: t.weight ?? DEFAULT_TERM_WEIGHT,
      note: t.note ?? '',
      aliases: t.aliases ?? [],
      blacklist: t.blacklist ?? [],
    }),
  )
}

function cleanWords(words: string[]): string[] {
  const out: string[] = []
  for (const w of words) {
    const word = w.trim()
    if (word && !out.includes(word)) out.push(word)
  }
  return out
}

/** The request body terms (trimmed; empty rows dropped). */
export function termsFromRows(rows: TermRow[]): Term[] {
  return rows
    .filter((r) => r.term.trim())
    .map((r) => {
      const term: Term = {
        term: r.term.trim(),
        weight: r.weight,
        aliases: cleanWords(r.aliases),
        blacklist: cleanWords(r.blacklist),
      }
      const note = r.note.trim()
      term.note = note || null
      return term
    })
}

export type RowProblem =
  | 'termLength'
  | 'weight'
  | 'noteLength'
  | 'tooManyAliases'
  | 'aliasLength'
  | 'tooManyBlacklist'
  | 'blacklistLength'
  | 'duplicate'

/** Problems of one row; `duplicate` is a warning (the backend merges duplicates). */
export function rowProblems(row: TermRow, duplicate = false): RowProblem[] {
  const out: RowProblem[] = []
  if ([...row.term.trim()].length > MAX_TERM_LENGTH) out.push('termLength')
  if (!isValidWeight(row.weight)) out.push('weight')
  if ([...row.note].length > MAX_NOTE_LENGTH) out.push('noteLength')
  const aliases = cleanWords(row.aliases)
  const blacklist = cleanWords(row.blacklist)
  if (aliases.length > MAX_WORDS) out.push('tooManyAliases')
  if (aliases.some((w) => [...w].length > MAX_TERM_LENGTH)) out.push('aliasLength')
  if (blacklist.length > MAX_WORDS) out.push('tooManyBlacklist')
  if (blacklist.some((w) => [...w].length > MAX_TERM_LENGTH)) out.push('blacklistLength')
  if (duplicate) out.push('duplicate')
  return out
}

/** Row keys whose term repeats an earlier row's (case-insensitive, like `merge_terms`). */
export function duplicateKeys(rows: TermRow[]): Set<number> {
  const seen = new Set<string>()
  const dup = new Set<number>()
  for (const r of rows) {
    const k = r.term.trim().toLowerCase()
    if (!k) continue
    if (seen.has(k)) dup.add(r.key)
    seen.add(k)
  }
  return dup
}

export type SetProblem = 'nameEmpty' | 'nameLength' | 'tooManyTerms'

export function setProblems(name: string, rows: TermRow[]): SetProblem[] {
  const out: SetProblem[] = []
  const n = name.trim()
  if (!n) out.push('nameEmpty')
  else if ([...n].length > MAX_NAME_LENGTH) out.push('nameLength')
  const unique = rows.filter((r) => r.term.trim()).length - duplicateKeys(rows).size
  if (unique > MAX_TERMS_PER_SET) out.push('tooManyTerms')
  return out
}

/** Whether the draft can be saved (warnings such as duplicates do not block). */
export function isSavable(name: string, rows: TermRow[]): boolean {
  if (setProblems(name, rows).length) return false
  return rows.every((r) => !r.term.trim() || rowProblems(r).length === 0)
}

export function boostedCount(rows: TermRow[]): number {
  return rows.filter((r) => r.term.trim() && r.weight === BOOSTED_WEIGHT).length
}

/** Collapse line breaks and tabs: a note must stay on its line. */
function flat(text: string): string {
  return text.split(/\s+/).filter(Boolean).join(' ')
}

/** A word that the line syntax cannot carry (it would be read back differently). */
function unsafeWord(word: string): boolean {
  return word.includes('|') || word.includes('~~~')
}

export interface SerializedTerms {
  text: string
  /** Terms that will not import back exactly (a `|` / `~~~` in a word, a leading `#`). */
  lossy: string[]
}

/** One line per term in the asr-hotword syntax; a `#` header names the set. */
export function serializeTerms(name: string, terms: Term[]): SerializedTerms {
  const lossy: string[] = []
  const lines = [`# ${flat(name)}`, '# 术语 | 别名 ~~~ 屏蔽词, 权重(1-5 / 50), 备注']
  for (const t of terms) {
    const aliases = t.aliases ?? []
    const blacklist = t.blacklist ?? []
    if ([t.term, ...aliases, ...blacklist].some(unsafeWord) || t.term.startsWith('#')) {
      lossy.push(t.term)
    }
    let head = [t.term, ...aliases].map(flat).join(' | ')
    if (blacklist.length) head += ` ~~~ ${blacklist.map(flat).join(' | ')}`
    const note = flat(t.note ?? '')
    const fields = [head, String(t.weight ?? DEFAULT_TERM_WEIGHT), ...(note ? [note] : [])]
    const sep = /[,，]/.test(head) ? '\t' : ', '
    lines.push(fields.join(sep))
  }
  return { text: `${lines.join('\n')}\n`, lossy }
}

/** A file name for the export (`/` and other reserved characters replaced). */
export function exportFileName(name: string): string {
  const safe = flat(name).replace(/[\\/:*?"<>|]/g, '_') || 'terms'
  return `${safe}.txt`
}
