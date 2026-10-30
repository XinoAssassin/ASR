import { describe, expect, it } from 'vitest'

import {
  boostedCount,
  duplicateKeys,
  exportFileName,
  isSavable,
  newRow,
  rowProblems,
  rowsFromTerms,
  serializeTerms,
  setProblems,
  termsFromRows,
} from './termSet'

describe('term rows', () => {
  it('round-trips terms and cleans words like the backend', () => {
    const rows = rowsFromTerms([{ term: 'Vue', weight: 4, note: 'fw', aliases: ['view'], blacklist: [] }])
    rows.push(newRow({ term: '  Pinia ', aliases: [' 皮尼亚 ', '皮尼亚', ''], note: '  ' }))
    rows.push(newRow()) // empty rows are dropped
    expect(termsFromRows(rows)).toEqual([
      { term: 'Vue', weight: 4, note: 'fw', aliases: ['view'], blacklist: [] },
      { term: 'Pinia', weight: 3, note: null, aliases: ['皮尼亚'], blacklist: [] },
    ])
  })

  it('validates like asr.providers.base.Term', () => {
    expect(rowProblems(newRow({ term: 'ok', weight: 50 }))).toEqual([])
    expect(rowProblems(newRow({ term: 'x'.repeat(101) }))).toContain('termLength')
    expect(rowProblems(newRow({ term: 'x', weight: 6 }))).toContain('weight')
    expect(rowProblems(newRow({ term: 'x', note: 'n'.repeat(501) }))).toContain('noteLength')
    const many = Array.from({ length: 21 }, (_, i) => `a${i}`)
    expect(rowProblems(newRow({ term: 'x', aliases: many }))).toContain('tooManyAliases')
    expect(rowProblems(newRow({ term: 'x', blacklist: ['b'.repeat(101)] }))).toContain('blacklistLength')
    // 100 CJK characters are fine (code points, not bytes)
    expect(rowProblems(newRow({ term: '字'.repeat(100) }))).toEqual([])
  })

  it('flags case-insensitive duplicates as warnings only', () => {
    const rows = [newRow({ term: 'Vue' }), newRow({ term: 'vue ' }), newRow({ term: 'Pinia' })]
    expect(duplicateKeys(rows)).toEqual(new Set([rows[1]!.key]))
    expect(isSavable('Set', rows)).toBe(true)
  })

  it('checks the set name and size', () => {
    expect(setProblems('  ', [])).toEqual(['nameEmpty'])
    expect(setProblems('n'.repeat(101), [])).toEqual(['nameLength'])
    const rows = Array.from({ length: 1001 }, (_, i) => newRow({ term: `t${i}` }))
    expect(setProblems('ok', rows)).toEqual(['tooManyTerms'])
    expect(isSavable('ok', rows)).toBe(false)
    expect(isSavable('ok', [newRow({ term: 'a', weight: 7 })])).toBe(false)
  })

  it('counts boosted terms', () => {
    expect(boostedCount([newRow({ term: 'a', weight: 50 }), newRow({ weight: 50 }), newRow({ term: 'b' })])).toBe(1)
  })
})

describe('serializeTerms', () => {
  it('writes the asr-hotword line syntax read by parse_terms', () => {
    const { text, lossy } = serializeTerms('客户 A', [
      { term: '通义千问', weight: 50, note: '产品\n名', aliases: ['通益千问', '同意千问'], blacklist: ['同意'] },
      { term: 'Kubernetes', weight: 4, aliases: [], blacklist: [] },
      { term: 'a,b', weight: 3, note: 'x, y', aliases: [], blacklist: [] },
    ])
    expect(text.split('\n')).toEqual([
      '# 客户 A',
      '# 术语 | 别名 ~~~ 屏蔽词, 权重(1-5 / 50), 备注',
      '通义千问 | 通益千问 | 同意千问 ~~~ 同意, 50, 产品 名',
      'Kubernetes, 4',
      'a,b\t3\tx, y',
      '',
    ])
    expect(lossy).toEqual([])
  })

  it('reports terms that cannot round-trip', () => {
    const { lossy } = serializeTerms('s', [
      { term: '#tag', weight: 3, aliases: [], blacklist: [] },
      { term: 'ok', weight: 3, aliases: ['a|b'], blacklist: [] },
    ])
    expect(lossy).toEqual(['#tag', 'ok'])
  })

  it('makes a safe file name', () => {
    expect(exportFileName('客户/项目: A')).toBe('客户_项目_ A.txt')
    expect(exportFileName('  ')).toBe('terms.txt')
  })
})
