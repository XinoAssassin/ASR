import { describe, expect, it } from 'vitest'

import { findActiveSegment } from './segments'

const segs = [
  { start: 0, end: 2 },
  { start: 2, end: 5 },
  { start: 6, end: 9 },
  { start: 7, end: 8 },
]

describe('findActiveSegment', () => {
  it('finds the covering segment', () => {
    expect(findActiveSegment(segs, 0)).toBe(0)
    expect(findActiveSegment(segs, 2)).toBe(1)
    expect(findActiveSegment(segs, 4.99)).toBe(1)
  })

  it('returns -1 in gaps and outside', () => {
    expect(findActiveSegment(segs, 5.5)).toBe(-1)
    expect(findActiveSegment(segs, -1)).toBe(-1)
    expect(findActiveSegment(segs, 10)).toBe(-1)
    expect(findActiveSegment([], 1)).toBe(-1)
  })

  it('prefers the later of overlapping segments', () => {
    expect(findActiveSegment(segs, 7.5)).toBe(3)
    expect(findActiveSegment(segs, 8.5)).toBe(2)
  })
})
