import { describe, expect, it } from 'vitest'

import { parseContentDispositionFilename as parse } from './contentDisposition'

describe('parseContentDispositionFilename', () => {
  it('prefers filename* (UTF-8)', () => {
    expect(
      parse(`attachment; filename="meeting.srt"; filename*=UTF-8''%E4%BC%9A%E8%AE%AE.srt`),
    ).toBe('会议.srt')
  })

  it('reads quoted and bare filenames', () => {
    expect(parse('attachment; filename="a b; c.txt"')).toBe('a b; c.txt')
    expect(parse('attachment; filename=plain.vtt')).toBe('plain.vtt')
    expect(parse('attachment; filename="say \\"hi\\".json"')).toBe('say "hi".json')
  })

  it('decodes ISO-8859-1 extended values', () => {
    expect(parse("attachment; filename*=iso-8859-1'en'%E9t%E9.txt")).toBe('été.txt')
  })

  it('strips paths and handles missing values', () => {
    expect(parse('attachment; filename="../../etc/passwd"')).toBe('passwd')
    expect(parse('attachment')).toBeNull()
    expect(parse(null)).toBeNull()
    expect(parse("attachment; filename*=UTF-8''%E4%ZZ")).toBeNull()
  })
})
