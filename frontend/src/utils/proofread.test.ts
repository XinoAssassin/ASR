import { describe, expect, it } from 'vitest'

import type { Correction, Segment } from '@/api/endpoints'

import { correctionMarks, pieces, searchMarks, termMarks } from './highlight'
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
