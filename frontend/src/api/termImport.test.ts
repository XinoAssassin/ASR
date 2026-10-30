import { describe, expect, it, vi } from 'vitest'

vi.stubGlobal('window', { localStorage: { getItem: () => null } })
const { importLineErrors, TermImportError } = await import('./endpoints')
const { ApiError } = await import('./errors')

describe('importLineErrors', () => {
  it('reads the per-line errors of parse_terms (FastAPI shape)', () => {
    const body = {
      detail: [
        { loc: ['body', 'text', 3], msg: "line 3: weight 'x' is not a number", type: 'value_error', input: 'Vue, x' },
        { loc: ['body', 'text', 7], msg: 'line 7: weight must be 1..5 or 50', input: 'A, 9' },
        { loc: ['body', 'name'], msg: 'String should have at least 1 character' },
      ],
    }
    expect(importLineErrors(body)).toEqual([
      { line: 3, message: "weight 'x' is not a number", input: 'Vue, x' },
      { line: 7, message: 'weight must be 1..5 or 50', input: 'A, 9' },
    ])
  })

  it('returns nothing for other error bodies', () => {
    expect(importLineErrors({ detail: 'name must not be blank' })).toEqual([])
    expect(importLineErrors(null)).toEqual([])
  })

  it('TermImportError is an ApiError', () => {
    const e = new TermImportError(422, 'x', [])
    expect(e).toBeInstanceOf(ApiError)
    expect(e.status).toBe(422)
  })
})
