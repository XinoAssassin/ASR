/**
 * Cross-layer offset fixture (ARCH_REVIEW §4-e), shared with the backend
 * (`backend/tests/test_offsets_fixture.py`, which documents the JSON shape). The backend checks
 * the edit results in code points; this side checks that the SPA's UTF-16 ↔ code-point conversion
 * lands on the same characters.
 */
import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

import { codePointLength, codePointToUtf16, utf16ToCodePoint } from './text'

interface FixtureCorrection {
  id: string
  segment_id: string
  before: string
  after: string
  status: string
  offset: number | null
  offset_utf16?: number | null
}

interface FixtureSegment {
  id: string
  text: string
}

interface FixtureOp {
  op: string
  segment_id?: string
  at?: number
}

interface FixtureCase {
  name: string
  ops: FixtureOp[]
  expect:
    | { segments: FixtureSegment[]; corrections: FixtureCorrection[] }
    | { error: string }
}

interface Fixture {
  version: number
  initial: { segments: FixtureSegment[]; corrections: FixtureCorrection[] }
  cases: FixtureCase[]
  positions: { text: string; pairs: [number, number][] }[]
}

const fixture = JSON.parse(
  readFileSync(new URL('../../../backend/tests/fixtures/offsets.json', import.meta.url), 'utf-8'),
) as Fixture

const shown = (c: { status: string; before: string; after: string }): string =>
  c.status === 'applied' || c.status === 'accepted' ? c.after : c.before

describe('offsets fixture', () => {
  it('has the expected version', () => {
    expect(fixture.version).toBe(1)
  })

  it('converts every code-point boundary both ways', () => {
    for (const { text, pairs } of fixture.positions) {
      expect(codePointLength(text)).toBe(pairs.length - 1)
      for (const [utf16, cp] of pairs) {
        expect(codePointToUtf16(text, cp)).toBe(utf16)
        expect(utf16ToCodePoint(text, utf16)).toBe(cp)
      }
    }
  })

  it('places the initial corrections on their text', () => {
    const texts = new Map(fixture.initial.segments.map((s) => [s.id, s.text]))
    for (const c of fixture.initial.corrections) {
      const text = texts.get(c.segment_id) ?? ''
      const start = codePointToUtf16(text, c.offset ?? -1)
      expect(start).toBe(c.offset_utf16)
      expect(text.slice(start, start + shown(c).length)).toBe(shown(c))
    }
  })

  const byId = new Map(fixture.initial.corrections.map((c) => [c.id, c]))
  for (const testCase of fixture.cases) {
    const result = testCase.expect
    if ('error' in result) continue
    it(`${testCase.name}: expected offsets point at the correction text`, () => {
      const texts = new Map(result.segments.map((s) => [s.id, s.text]))
      for (const c of result.corrections) {
        if (c.offset == null) {
          expect(c.offset_utf16).toBeNull()
          continue
        }
        const text = texts.get(c.segment_id) ?? ''
        const start = codePointToUtf16(text, c.offset)
        expect(start).toBe(c.offset_utf16)
        const source = byId.get(c.id)
        expect(source).toBeDefined()
        if (source) {
          const visible = shown({ ...source, status: c.status })
          expect(text.slice(start, start + visible.length)).toBe(visible)
        }
      }
    })
  }

  it('split positions sent as code points cut the UTF-16 text where the backend does', () => {
    const initial = new Map(fixture.initial.segments.map((s) => [s.id, s.text]))
    for (const testCase of fixture.cases) {
      const [op] = testCase.ops
      const result = testCase.expect
      if (testCase.ops.length !== 1 || op?.op !== 'split' || 'error' in result) continue
      const text = initial.get(op.segment_id ?? '') ?? ''
      const at = op.at ?? 0
      const cut = codePointToUtf16(text, at)
      expect(utf16ToCodePoint(text, cut)).toBe(at) // what the editor would send back
      const left = result.segments.find((s) => s.id === op.segment_id)
      const right = result.segments.find((s) => s.id === `${op.segment_id}-1`)
      expect(left?.text).toBe(text.slice(0, cut).trimEnd())
      expect(right?.text).toBe(text.slice(cut).trimStart())
    }
  })
})
