import { describe, expect, it } from 'vitest'

import type { EnvironmentOut } from '@/api/endpoints'

import { envFactRows, envVariable } from './envFacts'

const env: EnvironmentOut = {
  api_key_required: true,
  allow_unauthenticated_network: false,
  cors_origins: ['http://localhost:5173'],
  allowed_hosts: [],
  upload_roots: ['/data/uploads'],
  data_dir: '/data',
  model_cache_dir: '/models',
  decoder: 'pyav',
  torch_threads: null,
  local_dtype: 'auto',
  llm_model: 'qwen-plus',
  log_level: 'INFO',
  log_format: 'text',
  log_file: null,
}

describe('envFactRows', () => {
  it('orders rows, types values and names the variables', () => {
    const rows = envFactRows(env, { 'environment.cors_origins': 'ASR_CORS_ORIGINS' })
    expect(rows.map((r) => r.key).slice(0, 3)).toEqual([
      'api_key_required',
      'allow_unauthenticated_network',
      'cors_origins',
    ])
    const by = Object.fromEntries(rows.map((r) => [r.key, r]))
    expect(by.api_key_required!.value).toEqual({ kind: 'bool', value: true })
    expect(by.cors_origins!.value).toEqual({ kind: 'list', value: ['http://localhost:5173'] })
    expect(by.cors_origins!.variable).toBe('ASR_CORS_ORIGINS')
    expect(by.allowed_hosts!.value).toEqual({ kind: 'none' })
    expect(by.torch_threads!.value).toEqual({ kind: 'auto' })
    expect(by.log_file!.value).toEqual({ kind: 'none' })
    expect(by.log_level!.labelKey).toBe('logLevel')
    expect(by.data_dir!.variable).toBeNull()
  })

  it('keeps fields the backend adds later', () => {
    const rows = envFactRows({ ...env, new_flag: 'x' } as EnvironmentOut, {})
    expect(rows[rows.length - 1]!.key).toBe('new_flag')
  })
})

it('envVariable looks up dotted fields', () => {
  expect(envVariable({ 'local.model_cache_dir': 'ASR_MODEL_CACHE_DIR' }, 'local.model_cache_dir')).toBe(
    'ASR_MODEL_CACHE_DIR',
  )
  expect(envVariable(undefined, 'x')).toBeNull()
})
