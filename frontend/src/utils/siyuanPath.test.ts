import { describe, expect, it } from 'vitest'

import {
  DEFAULT_PATH_TEMPLATE,
  exportLocaleOf,
  pathTemplateProblem,
  pathValues,
  renderPathTemplate,
  stemOf,
} from './siyuanPath'

describe('renderPathTemplate (mirrors asr.export.siyuan.render_path)', () => {
  it('fills placeholders per segment', () => {
    expect(renderPathTemplate(DEFAULT_PATH_TEMPLATE, { date: '2026-10-30', title: '周会' })).toBe(
      '/会议记录/2026-10-30 周会',
    )
  })

  it('never lets a value add a level and drops empty segments', () => {
    expect(renderPathTemplate('/{source}/x', { source: 'a/b' })).toBe('/a／b/x')
    expect(renderPathTemplate('/{title}/{date}', { title: '', date: 'd' })).toBe('/d')
    expect(renderPathTemplate('/{title}', {})).toBeNull()
  })

  it('collapses line breaks and whitespace', () => {
    expect(renderPathTemplate('/{title}', { title: ' a\n\tb  c ' })).toBe('/a b c')
  })

  it('caps a segment at 200 characters', () => {
    const out = renderPathTemplate('/{title}', { title: '字'.repeat(300) })!
    expect([...out].length).toBe(201)
  })
})

describe('pathTemplateProblem (mirrors validate_path_template)', () => {
  it('accepts valid templates and the empty reset', () => {
    expect(pathTemplateProblem('/会议记录/{date} {title}')).toBeNull()
    expect(pathTemplateProblem('/{provider}/{time} {source}')).toBeNull()
    expect(pathTemplateProblem('')).toBeNull()
  })

  it('names the problem', () => {
    expect(pathTemplateProblem('会议/{title}')).toEqual({ reason: 'slash' })
    expect(pathTemplateProblem('/{name}')).toEqual({ reason: 'placeholder', name: 'name' })
    expect(pathTemplateProblem('/{title')).toEqual({ reason: 'braces' })
    expect(pathTemplateProblem('/')).toEqual({ reason: 'empty' })
  })
})

describe('pathValues', () => {
  it('derives title / source / date / time like export_source', () => {
    const v = pathValues({
      sourceName: 'C:\\rec\\周会.m4a',
      createdAt: new Date(2026, 9, 30, 9, 5).toISOString(),
      provider: 'cloud',
    })
    expect(v).toEqual({ title: '周会', source: '周会.m4a', date: '2026-10-30', time: '09:05', provider: 'cloud' })
  })

  it('treats the fallback stem "transcript" as no title', () => {
    expect(pathValues({ sourceName: null, createdAt: null, provider: 'local' }).title).toBe('')
    expect(pathValues({ sourceName: 'transcript.wav', createdAt: null, provider: 'local' }).title).toBe('')
    expect(stemOf('.env')).toBe('.env')
  })
})

it('maps the UI locale to the export locale', () => {
  expect(exportLocaleOf('zh-CN')).toBe('zh')
  expect(exportLocaleOf('en')).toBe('en')
})
