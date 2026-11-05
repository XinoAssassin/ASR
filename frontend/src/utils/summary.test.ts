import { describe, expect, it } from 'vitest'

import { resolveRefs, summaryActions, summaryEntries, type RefSegment } from './summary'

const seg = (id: string, start: number, end: number, text = id, speaker_id = 'SPEAKER_00'): RefSegment => ({
  id,
  start,
  end,
  text,
  speaker_id,
})

describe('summaryEntries', () => {
  it('reads current objects and keeps refs', () => {
    expect(summaryEntries([{ text: 'A', refs: ['s1', 's2'] }, { text: 'B' }])).toEqual([
      { text: 'A', refs: ['s1', 's2'] },
      { text: 'B', refs: [] },
    ])
  })

  it('accepts pre-citation plain strings and drops junk', () => {
    expect(summaryEntries(['old point', '', null, 3, { refs: ['x'] }, { text: ' ', refs: [] }])).toEqual([
      { text: 'old point', refs: [] },
    ])
    expect(summaryEntries(undefined)).toEqual([])
  })

  it('ignores non-string refs', () => {
    expect(summaryEntries([{ text: 'A', refs: ['s1', 2, null, ''] }])).toEqual([{ text: 'A', refs: ['s1'] }])
  })
})

describe('summaryActions', () => {
  it('normalises owner / due and tolerates missing refs', () => {
    expect(
      summaryActions([
        { task: 'Ship', owner: '张三', due: null, refs: ['s3'] },
        { task: 'Old', owner: '', due: '周五' },
        { owner: 'x' },
      ]),
    ).toEqual([
      { task: 'Ship', owner: '张三', due: null, refs: ['s3'] },
      { task: 'Old', owner: null, due: '周五', refs: [] },
    ])
  })
})

describe('resolveRefs', () => {
  const original = [seg('s1', 0, 4), seg('s2', 4, 8), seg('s3', 8, 12), seg('s4', 12, 16)]

  it('resolves live ids to their own start, in the given order', () => {
    const r = resolveRefs(['s3', 's1'], original, original)
    expect(r.map((x) => [x.segmentId, x.start])).toEqual([
      ['s3', 8],
      ['s1', 0],
    ])
  })

  it('maps a ref merged away to the segment that now covers its time', () => {
    // s2 merged into s1 (s1 keeps its id and now spans 0..8).
    const current = [seg('s1', 0, 8, 's1 s2'), seg('s3', 8, 12), seg('s4', 12, 16)]
    const r = resolveRefs(['s2', 's4'], current, original)
    expect(r).toEqual([
      { ref: 's2', segmentId: 's1', start: 4, speakerId: 'SPEAKER_00', text: 's1 s2' },
      { ref: 's4', segmentId: 's4', start: 12, speakerId: 'SPEAKER_00', text: 's4' },
    ])
  })

  it('collapses refs that land in the same merged segment and skips unknown ids', () => {
    const current = [seg('s1', 0, 12, 'all')]
    const r = resolveRefs(['s1', 's2', 'nope', 's3'], current, original)
    expect(r.map((x) => x.segmentId)).toEqual(['s1'])
  })

  it('falls back to the segment before a gap', () => {
    const current = [seg('s1', 0, 3), seg('s4', 12, 16)]
    expect(resolveRefs(['s2'], current, original)[0]?.segmentId).toBe('s1')
  })

  it('returns nothing without segments', () => {
    expect(resolveRefs(['s1'], [], original)).toEqual([])
  })
})
