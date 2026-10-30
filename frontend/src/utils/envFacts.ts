/**
 * Rows for the Settings page's "set by environment variables" section (`SettingsOut.environment`
 * and `env_only`, ARCH_REVIEW §4-f). Values stay typed so the view can localise yes / no / none.
 */
import type { EnvironmentOut } from '@/api/endpoints'

export type EnvValue =
  | { kind: 'bool'; value: boolean }
  | { kind: 'text'; value: string }
  | { kind: 'list'; value: string[] }
  | { kind: 'none' }
  | { kind: 'auto' }

export interface EnvFactRow {
  key: keyof EnvironmentOut
  /** Key under `envFacts.` in the locale files. */
  labelKey: string
  value: EnvValue
  /** Environment variable name from `env_only`, when the backend names one. */
  variable: string | null
}

/** Display order; fields the backend adds later are appended after these. */
const ORDER: (keyof EnvironmentOut)[] = [
  'api_key_required',
  'allow_unauthenticated_network',
  'cors_origins',
  'allowed_hosts',
  'upload_roots',
  'data_dir',
  'model_cache_dir',
  'decoder',
  'torch_threads',
  'local_dtype',
  'llm_model',
  'log_level',
  'log_format',
  'log_file',
]

/** Unset means "automatic" for these, "(none)" for everything else. */
const AUTO_WHEN_UNSET = new Set<string>(['torch_threads'])

function camel(key: string): string {
  return key.replace(/_([a-z])/g, (_m, c: string) => c.toUpperCase())
}

function toValue(key: string, raw: unknown): EnvValue {
  if (raw === null || raw === undefined || raw === '') {
    return AUTO_WHEN_UNSET.has(key) ? { kind: 'auto' } : { kind: 'none' }
  }
  if (typeof raw === 'boolean') return { kind: 'bool', value: raw }
  if (Array.isArray(raw)) {
    const items = raw.map(String).filter(Boolean)
    return items.length ? { kind: 'list', value: items } : { kind: 'none' }
  }
  return { kind: 'text', value: String(raw) }
}

export function envFactRows(env: EnvironmentOut, envOnly: Record<string, string>): EnvFactRow[] {
  const keys = [
    ...ORDER.filter((k) => k in env),
    ...(Object.keys(env) as (keyof EnvironmentOut)[]).filter((k) => !ORDER.includes(k)),
  ]
  return keys.map((key) => ({
    key,
    labelKey: camel(key),
    value: toValue(key, env[key]),
    variable: envOnly[`environment.${key}`] ?? null,
  }))
}

/** The environment variable that sets a settings field (`local.model_cache_dir`), or null. */
export function envVariable(envOnly: Record<string, string> | undefined, field: string): string | null {
  return envOnly?.[field] ?? null
}
