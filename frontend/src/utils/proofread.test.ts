import { describe, expect, it } from 'vitest'

import type { Correction, Segment } from '@/api/endpoints'

import {
  correctionContext,
  correctionMarks,
  occurrences,
  pieces,
  PiecesMemo,
  searchMarks,
  termMarks,
} from './highlight'
import { findMatches, withAlias } from './search'

const corr = (over: Partial<Correction>): Correction =>
  ({
    id: 'c1',
    segment_id: 's',
    source: 'term',
    before: '汤泡',
    after: 'Townfall',
    score: 1,
    status: 'applied',
    offset: null,
    ...over,
  }) as Correction

describe('highlight pieces', () => {
  it('cuts at overlapping marks and keeps plain text', () => {
    const text = '我玩Townfall很久'
    const marks = [...termMarks(text, ['townfall']), ...searchMarks(text, 'Town')]
    expect(pieces(text, marks)).toEqual([
      { text: '我玩', kinds: [], refs: [] },
      { text: 'Town', kinds: ['term', 'search'], refs: [] },
      { text: 'fall', kinds: ['term'], refs: [] },
      { text: '很久', kinds: [], refs: [] },
    ])
  })

  it('marks applied corrections on the new text and suggestions on the old', () => {
    const text = 'Townfall很好玩'
    const marks = correctionMarks(text, [
      corr({}),
      corr({ id: 'c2', source: 'llm', status: 'suggested', before: '很好玩', after: '真好玩', offset: 8 }),
      corr({ id: 'c3', status: 'rejected' }),
    ])
    expect(marks).toEqual([
      { start: 0, end: 8, kind: 'correction', ref: 'c1' },
      { start: 8, end: 11, kind: 'suggestion', ref: 'c2' },
    ])
  })

  it('shows the text around a correction', () => {
    const text = '我们之前聊过的那个Townfall真的很好玩，大家都喜欢'
    expect(correctionContext(text, corr({}), 4)).toEqual({ left: '…过的那个', hit: 'Townfall', right: '真的很好…' })
    // pending insertion: located by offset only
    expect(correctionContext('生化好玩', corr({ before: '', after: '危机', status: 'suggested', offset: 2 }), 4)).toEqual({
      left: '生化',
      hit: '',
      right: '好玩',
    })
    expect(correctionContext('别的', corr({ offset: null }))).toBeNull()
  })

  it('handles empty text and no marks', () => {
    expect(pieces('', [])).toEqual([])
    expect(pieces('abc', [])).toEqual([{ text: 'abc', kinds: [], refs: [] }])
  })
})

describe('search and aliases', () => {
  const seg = (id: string, text: string) => ({ id, text }) as Segment

  it('finds case-insensitive matches in order', () => {
    expect(findMatches([seg('a', 'Hello hello'), seg('b', 'no'), seg('c', 'HELLO')], 'hello')).toEqual([
      { segmentId: 'a', start: 0, end: 5 },
      { segmentId: 'a', start: 6, end: 11 },
      { segmentId: 'c', start: 0, end: 5 },
    ])
    expect(findMatches([seg('a', 'x')], '  ')).toEqual([])
  })

  it('keeps indices right where lower-casing changes the length, and treats the query literally', () => {
    // 'İ'.toLowerCase() is two code units: mapping lower-cased indices back used to shift marks.
    const text = 'İİ abc ABC'
    expect(occurrences(text, 'abc')).toEqual([
      { start: 3, end: 6 },
      { start: 7, end: 10 },
    ])
    expect(searchMarks(text, 'ABC').map((m) => text.slice(m.start, m.end))).toEqual(['abc', 'ABC'])
    expect(findMatches([seg('a', 'İİ abc')], 'abc')).toEqual([{ segmentId: 'a', start: 3, end: 6 }])
    expect(occurrences('a.b a+b (x)', 'a+b')).toEqual([{ start: 4, end: 7 }])
    expect(occurrences('a.b a+b (x)', '(x)')).toEqual([{ start: 8, end: 11 }])
    expect(occurrences('Ab ab', 'ab', false)).toEqual([{ start: 3, end: 5 }])
  })

  it('adds an alias to an existing term or creates the term', () => {
    const terms = [{ term: 'Townfall', weight: 4, aliases: ['汤泡'], blacklist: [] }]
    expect(withAlias(terms, 'townfall', '汤炮')[0]!.aliases).toEqual(['汤泡', '汤炮'])
    expect(withAlias(terms, 'Townfall', '汤泡')).toEqual(terms) // already known
    expect(withAlias(terms, '寂静岭', '基金岭')[1]).toEqual({
      term: '寂静岭',
      weight: 3,
      aliases: ['基金岭'],
      blacklist: [],
    })
  })
})

describe('PiecesMemo', () => {
  const base = { terms: ['Townfall'], corrections: [] as Correction[], query: '', current: null }

  it('returns the same array while the inputs are unchanged', () => {
    const memo = new PiecesMemo()
    const a = memo.get('s', { ...base, text: '玩Townfall' })
    expect(a.map((p) => p.kinds)).toEqual([[], ['term']])
    // a fresh but equal terms array / corrections array does not invalidate
    expect(memo.get('s', { ...base, terms: ['Townfall'], corrections: [], text: '玩Townfall' })).toBe(a)
    expect(memo.get('s', { ...base, text: '玩Townfall!' })).not.toBe(a)
  })

  it('layers search marks on top and keeps base pieces for segments without hits', () => {
    const memo = new PiecesMemo()
    const plain = memo.get('s', { ...base, text: '玩Townfall' })
    expect(memo.get('s', { ...base, text: '玩Townfall', query: 'zzz' })).toBe(plain)
    const hit = memo.get('s', { ...base, text: '玩Townfall', query: 'town' })
    expect(hit.find((p) => p.kinds.includes('search'))?.text).toBe('Town')
    expect(memo.get('s', { ...base, text: '玩Townfall', query: 'town' })).toBe(hit)
    const current = memo.get('s', { ...base, text: '玩Townfall', query: 'town', current: { start: 1, end: 5 } })
    expect(current).not.toBe(hit)
    expect(current.find((p) => p.kinds.includes('current'))?.text).toBe('Town')
    expect(memo.get('s', { ...base, text: '玩Townfall' })).toBe(plain)
  })

  it('invalidates on a correction change and forgets removed segments', () => {
    const memo = new PiecesMemo()
    const text = 'Townfall很好玩'
    const c = corr({ segment_id: 's', status: 'suggested', before: '很好玩', after: '真好玩', offset: 8 })
    const a = memo.get('s', { ...base, text, corrections: [c] })
    expect(memo.get('s', { ...base, text, corrections: [{ ...c }] })).toBe(a)
    expect(memo.get('s', { ...base, text, corrections: [{ ...c, status: 'rejected' }] })).not.toBe(a)
    memo.get('t', { ...base, text: 'x' })
    memo.retain(['t'])
    expect(memo.size).toBe(1)
  })
})
