import { describe, expect, it } from 'vitest'

import { parseTerms } from './terms'

describe('parseTerms', () => {
  it('parses terms with optional weights', () => {
    const { terms, errors } = parseTerms('Kubernetes\nPyTorch, 5\n\n# comment\n大模型，50\nKubernetes')
    expect(errors).toEqual([])
    expect(terms).toEqual([
      { term: 'Kubernetes', weight: 3, aliases: [], blacklist: [] },
      { term: 'PyTorch', weight: 5, aliases: [], blacklist: [] },
      { term: '大模型', weight: 50, aliases: [], blacklist: [] },
    ])
  })

  it('reports invalid weights and over-long terms', () => {
    const { terms, errors } = parseTerms(`ok\nbad,7\nbad2,x\n${'a'.repeat(101)}`)
    expect(terms).toEqual([{ term: 'ok', weight: 3, aliases: [], blacklist: [] }])
    expect(errors.map((e) => [e.line, e.reason])).toEqual([
      [2, 'weight'],
      [3, 'weight'],
      [4, 'length'],
    ])
  })

  it('keeps commas inside the term when the weight is empty', () => {
    expect(parseTerms('a,b,').terms).toEqual([{ term: 'a,b', weight: 3, aliases: [], blacklist: [] }])
  })

  it('parses the asr-hotword alias / blacklist syntax', () => {
    const { terms, errors } = parseTerms('Townfall | 汤泡 | 汤炮 ~~~ 泡面, 4\nClaude | cloud')
    expect(errors).toEqual([])
    expect(terms).toEqual([
      { term: 'Townfall', weight: 4, aliases: ['汤泡', '汤炮'], blacklist: ['泡面'] },
      { term: 'Claude', weight: 3, aliases: ['cloud'], blacklist: [] },
    ])
  })
})
