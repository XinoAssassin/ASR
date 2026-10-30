import { describe, expect, it } from 'vitest'

import type { SettingsOut } from '@/api/endpoints'

import {
  buildUpdate,
  canReset,
  changedFields,
  dropsLlmKey,
  isHttpUrl,
  inheritsFromCloud,
  llmTestKind,
  llmTargetHost,
  resetUpdate,
  serverValues,
  sourceOf,
  syncValues,
  type OnlineValues,
} from './onlineSettings'

/** Loose overrides: older backends omit `llm` / `sources` / `config_file`. */
function server(overrides: Record<string, unknown> = {}): SettingsOut {
  return {
    provider_default: 'cloud',
    api_key_required: false,
    upload_roots: [],
    env_only: {},
    environment: {} as SettingsOut['environment'],
    local: { asr_model: null, device: 'cpu', diarize_model: null, model_cache_dir: '/m' },
    cloud: {
      endpoint: 'https://dashscope.aliyuncs.com',
      model: 'qwen-audio-3.1-asr-flash-filetrans',
      file_transfer: 'temp_upload',
      api_key_set: true,
      api_key_masked: 'sk-****abcd',
      api_key_source: 'env',
    },
    llm: {
      provider: 'dashscope',
      endpoint: '',
      model: 'qwen-plus',
      api_key_set: false,
      api_key_masked: null,
    },
    sources: {
      'cloud.endpoint': 'default',
      'cloud.model': 'settings',
      'cloud.file_transfer': 'config',
      'cloud.api_key': 'env',
      'llm.provider': 'default',
      'llm.endpoint': 'default',
      'llm.model': 'settings',
      'llm.api_key': 'default',
    },
    config_file: { path: null, loaded: false, error: null, warnings: [] },
    ...overrides,
  } as SettingsOut
}

const base: OnlineValues = serverValues(server())
const noKeys = { cloud: '', llm: '' }

describe('sourceOf / canReset', () => {
  it('reads sources and only offers reset for UI overrides', () => {
    const s = server()
    expect(sourceOf(s, 'cloud.file_transfer')).toBe('config')
    expect(sourceOf(s, 'cloud.api_key')).toBe('env')
    expect(canReset(s, 'cloud.model')).toBe(true)
    expect(canReset(s, 'cloud.endpoint')).toBe(false)
    expect(canReset(s, 'cloud.file_transfer')).toBe(false)
  })

  it('tolerates older backends: no sources, only cloud.api_key_source', () => {
    const s = server({ sources: undefined })
    expect(sourceOf(s, 'cloud.model')).toBeNull()
    expect(canReset(s, 'cloud.model')).toBe(false)
    expect(sourceOf(s, 'cloud.api_key')).toBe('env')
    expect(sourceOf(null, 'llm.model')).toBeNull()
  })

  it('ignores unknown source values', () => {
    const s = server({ sources: { 'llm.model': 'weird' } })
    expect(sourceOf(s, 'llm.model')).toBeNull()
  })
})

describe('resetUpdate', () => {
  it('sends "" for exactly the reset field', () => {
    expect(resetUpdate('cloud.model')).toEqual({ cloud: { model: '' } })
    expect(resetUpdate('llm.endpoint')).toEqual({ llm: { endpoint: '' } })
    expect(resetUpdate('llm.api_key')).toEqual({ llm: { api_key: '' } })
  })
})

describe('serverValues', () => {
  it('defaults the LLM fields when the backend has none', () => {
    const v = serverValues(server({ llm: undefined }))
    expect(v.llmProvider).toBe('dashscope')
    expect(v.llmEndpoint).toBe('')
    expect(v.llmModel).toBe('')
  })
})

describe('buildUpdate', () => {
  it('is null when nothing changed (whitespace does not count)', () => {
    expect(buildUpdate(base, { ...base, llmModel: ' qwen-plus ' }, noKeys)).toBeNull()
    expect(changedFields(base, base)).toEqual([])
  })

  it('carries only the changed fields, trimmed', () => {
    const current = { ...base, llmProvider: 'openai' as const, llmEndpoint: ' http://127.0.0.1:8080/v1 ' }
    expect(buildUpdate(base, current, noKeys)).toEqual({
      llm: { provider: 'openai', endpoint: 'http://127.0.0.1:8080/v1' },
    })
  })

  it('an emptied field is sent as "" (clears the override)', () => {
    expect(buildUpdate(base, { ...base, cloudModel: '  ' }, noKeys)).toEqual({
      cloud: { model: '' },
    })
  })

  it('adds typed-in keys, never empty ones', () => {
    expect(buildUpdate(base, base, { cloud: ' sk-new ', llm: '' })).toEqual({
      cloud: { api_key: 'sk-new' },
    })
    expect(buildUpdate(base, base, { cloud: '', llm: 'k2' })).toEqual({ llm: { api_key: 'k2' } })
  })

  it('includes the default provider and cloud fields', () => {
    const current = { ...base, providerDefault: 'local' as const, cloudFileTransfer: 'public_url' as const }
    expect(buildUpdate(base, current, noKeys)).toEqual({
      provider_default: 'local',
      cloud: { file_transfer: 'public_url' },
    })
  })
})

describe('syncValues', () => {
  it('fills everything on the first load', () => {
    const empty = { ...base, cloudModel: '', llmModel: '' }
    expect(syncValues(empty, null, base)).toEqual(base)
  })

  it('updates untouched fields and keeps edited ones', () => {
    const current = { ...base, llmModel: 'typed' }
    const next = { ...base, llmModel: 'server-new', cloudModel: 'cloud-new' }
    const result = syncValues(current, base, next)
    expect(result.llmModel).toBe('typed')
    expect(result.cloudModel).toBe('cloud-new')
  })

  it('force overwrites the reset field only, or all after a save', () => {
    const current = { ...base, llmModel: 'typed', cloudModel: 'also typed' }
    const next = { ...base, llmModel: 'fallback' }
    const reset = syncValues(current, base, next, ['llmModel'])
    expect(reset.llmModel).toBe('fallback')
    expect(reset.cloudModel).toBe('also typed')
    expect(syncValues(current, base, next, 'all')).toEqual(next)
  })
})

describe('validation helpers', () => {
  it('isHttpUrl', () => {
    expect(isHttpUrl('http://127.0.0.1:8080/v1')).toBe(true)
    expect(isHttpUrl('https://api.example.com/v1')).toBe(true)
    expect(isHttpUrl('ftp://x')).toBe(false)
    expect(isHttpUrl('localhost:8080')).toBe(false)
    expect(isHttpUrl('')).toBe(false)
  })

  it('llmTargetHost: dashscope falls back to the cloud endpoint', () => {
    expect(llmTargetHost(base, 'https://dashscope.aliyuncs.com')).toBe('dashscope.aliyuncs.com')
    const openai = { ...base, llmProvider: 'openai' as const, llmEndpoint: 'http://gpu.lan:8080/v1' }
    expect(llmTargetHost(openai, 'https://dashscope.aliyuncs.com')).toBe('gpu.lan:8080')
    expect(llmTargetHost({ ...openai, llmEndpoint: '' }, 'https://x')).toBeNull()
  })
})

describe('DashScope inheritance', () => {
  it('an inherited DashScope LLM endpoint is shown empty (keeps inheriting)', () => {
    const s = server({
      llm: { provider: 'dashscope', endpoint: 'https://dashscope.aliyuncs.com', model: 'q', api_key_set: true, api_key_masked: 'sk-****abcd' },
    })
    expect(serverValues(s).llmEndpoint).toBe('')
    const own = server({
      llm: { provider: 'dashscope', endpoint: 'https://cn-beijing.maas.qianwenaiapi.com', model: 'q', api_key_set: true, api_key_masked: null },
      sources: { 'llm.endpoint': 'settings' },
    })
    expect(serverValues(own).llmEndpoint).toBe('https://cn-beijing.maas.qianwenaiapi.com')
  })

  it('inheritsFromCloud: dashscope with a default source only', () => {
    expect(inheritsFromCloud(server(), 'llm.api_key')).toBe(true)
    expect(inheritsFromCloud(server(), 'llm.endpoint')).toBe(true)
    expect(inheritsFromCloud(server({ sources: { 'llm.api_key': 'settings' } }), 'llm.api_key')).toBe(false)
    expect(inheritsFromCloud(server({ sources: { 'llm.endpoint': 'env' } }), 'llm.endpoint')).toBe(false)
    const openai = server({
      llm: { provider: 'openai', endpoint: 'http://x/v1', model: 'm', api_key_set: false, api_key_masked: null },
    })
    expect(inheritsFromCloud(openai, 'llm.api_key')).toBe(false)
  })

  it('dropsLlmKey: switching to / moving an OpenAI-compatible host', () => {
    const openai = { ...base, llmProvider: 'openai' as const, llmEndpoint: 'http://a:8080/v1' }
    expect(dropsLlmKey(base, openai)).toBe(true)
    expect(dropsLlmKey(openai, openai)).toBe(false)
    expect(dropsLlmKey(openai, { ...openai, llmEndpoint: 'http://b:8080/v1' })).toBe(true)
    expect(dropsLlmKey(openai, { ...openai, llmModel: 'other' })).toBe(false)
    expect(dropsLlmKey(openai, base)).toBe(false)
  })
})

describe('llmTestKind', () => {
  it('error / success / success without JSON mode', () => {
    expect(llmTestKind({ ok: false, message: 'refused' })).toBe('error')
    expect(llmTestKind({ ok: true, message: 'Model answered' })).toBe('success')
    expect(
      llmTestKind({ ok: true, message: 'Model answered (no JSON mode; answers are parsed from plain text)' }),
    ).toBe('warning')
  })
})
