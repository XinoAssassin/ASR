import { describe, expect, it } from 'vitest'

import type { Correction } from '@/api/endpoints'

import { correctionContext, correctionMarks } from './highlight'
import { codePointLength, codePointToUtf16, utf16ToCodePoint } from './text'

describe('code point / UTF-16 indices', () => {
  const text = 'a😀b𠀀c' // two astral characters, 7 UTF-16 units, 5 code points

  it('counts code points like Python len()', () => {
    expect(text.length).toBe(7)
    expect(codePointLength(text)).toBe(5)
    expect(codePointLength('中文 English')).toBe(10)
    expect(codePointLength('')).toBe(0)
  })

  it('converts both ways', () => {
    // UTF-16 index of 'b' is 3, its code-point index 2; 'c' is 6 / 4.
    expect(utf16ToCodePoint(text, 3)).toBe(2)
    expect(utf16ToCodePoint(text, 6)).toBe(4)
    expect(utf16ToCodePoint(text, 7)).toBe(5)
    expect(codePointToUtf16(text, 2)).toBe(3)
    expect(codePointToUtf16(text, 4)).toBe(6)
    expect(codePointToUtf16(text, 5)).toBe(7)
    for (let cp = 0; cp <= 5; cp++) expect(utf16ToCodePoint(text, codePointToUtf16(text, cp))).toBe(cp)
  })

  it('is the identity for BMP-only text and clamps out-of-range indices', () => {
    expect(utf16ToCodePoint('中文abc', 3)).toBe(3)
    expect(codePointToUtf16('中文abc', 3)).toBe(3)
    expect(utf16ToCodePoint(text, -4)).toBe(0)
    expect(utf16ToCodePoint(text, 99)).toBe(5)
    expect(codePointToUtf16(text, 99)).toBe(7)
  })

  it('treats an index inside a surrogate pair as before that character', () => {
    expect(utf16ToCodePoint(text, 2)).toBe(1) // between the halves of 😀
  })
})

describe('correction offsets are code points', () => {
  const corr = (over: Partial<Correction>): Correction =>
    ({
      id: 'c1',
      segment_id: 's',
      source: 'term',
      before: '汤泡',
      after: 'Town',
      status: 'applied',
      offset: null,
      ...over,
    }) as Correction

  it('places a mark after an emoji at the right UTF-16 position', () => {
    // 'Town' occurs twice; the server offset (code points) points at the second one.
    const text = '😀Town说Town'
    const c = corr({ offset: 6 }) // code points: 😀=0, T=1..n=4, 说=5, T=6
    const [mark] = correctionMarks(text, [c])
    expect(mark).toMatchObject({ start: 7, end: 11, kind: 'correction' })
    expect(correctionContext(text, c, 3)).toEqual({ left: '…wn说', hit: 'Town', right: '' })
  })
})
