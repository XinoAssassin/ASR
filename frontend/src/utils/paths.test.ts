import { describe, expect, it } from 'vitest'

import { isUnderRoot, rootOf } from './paths'

describe('isUnderRoot', () => {
  it('needs a separator after the root', () => {
    expect(isUnderRoot('/data/uploads', '/data/uploads')).toBe(true)
    expect(isUnderRoot('/data/uploads/a/b', '/data/uploads')).toBe(true)
    expect(isUnderRoot('/data/uploads/a', '/data/uploads/')).toBe(true)
    expect(isUnderRoot('/data/uploads2', '/data/uploads')).toBe(false)
    expect(isUnderRoot('/data/uploads2/x', '/data/uploads')).toBe(false)
    expect(isUnderRoot('', '/data')).toBe(false)
  })

  it('handles Windows paths and filesystem roots', () => {
    expect(isUnderRoot('D:\\Audio\\2026', 'D:\\Audio')).toBe(true)
    expect(isUnderRoot('D:\\Audio2', 'D:\\Audio')).toBe(false)
    expect(isUnderRoot('D:\\x', 'D:\\')).toBe(true)
    expect(isUnderRoot('/srv', '/')).toBe(true)
  })

  it('picks the deepest matching root', () => {
    const roots = [{ path: '/data' }, { path: '/data/uploads' }, { path: '/data/uploads2' }]
    expect(rootOf('/data/uploads2/a', roots)?.path).toBe('/data/uploads2')
    expect(rootOf('/data/uploads/a', roots)?.path).toBe('/data/uploads')
    expect(rootOf('/data/other', roots)?.path).toBe('/data')
    expect(rootOf('/elsewhere', roots)).toBeUndefined()
  })
})
