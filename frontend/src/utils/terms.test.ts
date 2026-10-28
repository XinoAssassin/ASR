import { describe, expect, it } from 'vitest'

import { parseTerms } from './terms'

describe('parseTerms', () => {
  it('parses terms with optional weights', () => {
    const { terms, errors } = parseTerms('Kubernetes\nPyTorch, 5\n\n# comment\n大模型，50\nKubernetes')
    expect(errors).toEqual([])
    expect(terms).toEqual([
      { term: 'Kubernetes', weight: 3 },
      { term: 'PyTorch', weight: 5 },
      { term: '大模型', weight: 50 },
    ])
  })

  it('reports invalid weights and over-long terms', () => {
    const { terms, errors } = parseTerms(`ok\nbad,7\nbad2,x\n${'a'.repeat(101)}`)
    expect(terms).toEqual([{ term: 'ok', weight: 3 }])
    expect(errors.map((e) => [e.line, e.reason])).toEqual([
      [2, 'weight'],
      [3, 'weight'],
      [4, 'length'],
    ])
  })

  it('keeps commas inside the term when the weight is empty', () => {
    expect(parseTerms('a,b,').terms).toEqual([{ term: 'a,b', weight: 3 }])
  })
})
