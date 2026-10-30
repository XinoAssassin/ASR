/**
 * Logic of the Settings page's "online services" card (cloud ASR + LLM): where each effective
 * value comes from (`SettingsOut.sources`), the request that resets a UI override, the update
 * that carries only the fields the user changed, and a per-field form sync that never clobbers
 * unsaved input. Precedence on the backend: UI override (`settings`) > config file > env > default;
 * in `PUT /api/settings` an omitted field is unchanged and `""` clears the override.
 */
import type {
  FileTransfer,
  LlmProviderName,
  ProviderName,
  SettingSource,
  SettingSourceKey,
  SettingsOut,
  SettingsUpdate,
} from '@/api/endpoints'

/** The editable (non-secret) values of the card, flat. Keys are trimmed when compared / sent. */
export interface OnlineValues {
  providerDefault: ProviderName
  cloudEndpoint: string
  cloudModel: string
  cloudFileTransfer: FileTransfer
  llmProvider: LlmProviderName
  llmEndpoint: string
  llmModel: string
}

export type OnlineField = keyof OnlineValues

/** Typed-in new keys (write-only; empty = leave the stored key alone). */
export interface NewKeys {
  cloud: string
  llm: string
}

export const ONLINE_FIELDS: readonly OnlineField[] = [
  'providerDefault',
  'cloudEndpoint',
  'cloudModel',
  'cloudFileTransfer',
  'llmProvider',
  'llmEndpoint',
  'llmModel',
]

/** Form field → its `sources` key (the default provider has no source). */
export const FIELD_SOURCE: Partial<Record<OnlineField, SettingSourceKey>> = {
  cloudEndpoint: 'cloud.endpoint',
  cloudModel: 'cloud.model',
  cloudFileTransfer: 'cloud.file_transfer',
  llmProvider: 'llm.provider',
  llmEndpoint: 'llm.endpoint',
  llmModel: 'llm.model',
}

const SOURCES: readonly SettingSource[] = ['default', 'env', 'config', 'settings']

/** Where `key`'s effective value comes from, or null when the backend does not say. */
export function sourceOf(
  server: Pick<SettingsOut, 'sources' | 'cloud'> | null | undefined,
  key: SettingSourceKey,
): SettingSource | null {
  const value = server?.sources?.[key]
  if (value && SOURCES.includes(value)) return value
  // Older backends only report the cloud key's source.
  if (key === 'cloud.api_key') {
    const legacy = server?.cloud.api_key_source
    if (legacy && SOURCES.includes(legacy)) return legacy
  }
  return null
}

/** A value set in the UI can be reset: clearing the override falls back to config / env / default. */
export function canReset(
  server: Pick<SettingsOut, 'sources' | 'cloud'> | null | undefined,
  key: SettingSourceKey,
): boolean {
  return sourceOf(server, key) === 'settings'
}

/** The update that clears the UI override of `key` (`""` = fall back). */
export function resetUpdate(key: SettingSourceKey): SettingsUpdate {
  const [section, field] = key.split('.') as ['cloud' | 'llm', string]
  return { [section]: { [field]: '' } } as SettingsUpdate
}

/**
 * The form values for what the server reports. For DashScope the backend reports the *effective*
 * LLM endpoint; when it is inherited from the cloud endpoint (source `default`) the field stays
 * empty ("use the cloud one", shown as the placeholder) so leaving it alone keeps inheriting.
 */
export function serverValues(server: SettingsOut): OnlineValues {
  const inheritsEndpoint = inheritsFromCloud(server, 'llm.endpoint')
  return {
    providerDefault: server.provider_default,
    cloudEndpoint: server.cloud.endpoint,
    cloudModel: server.cloud.model,
    cloudFileTransfer: server.cloud.file_transfer,
    llmProvider: server.llm?.provider ?? 'dashscope',
    llmEndpoint: inheritsEndpoint ? '' : (server.llm?.endpoint ?? ''),
    llmModel: server.llm?.model ?? '',
  }
}

function norm(value: string): string {
  return value.trim()
}

/** Fields whose (trimmed) value differs from the baseline. */
export function changedFields(baseline: OnlineValues, current: OnlineValues): OnlineField[] {
  return ONLINE_FIELDS.filter((f) => norm(current[f]) !== norm(baseline[f]))
}

/**
 * The update carrying only what changed (plus typed-in keys); null when nothing did. An emptied
 * text field is sent as `""`, which clears the UI override (the backend falls back).
 */
export function buildUpdate(
  baseline: OnlineValues,
  current: OnlineValues,
  keys: NewKeys,
): SettingsUpdate | null {
  const changed = new Set(changedFields(baseline, current))
  const cloud: NonNullable<SettingsUpdate['cloud']> = {}
  const llm: NonNullable<SettingsUpdate['llm']> = {}
  const update: SettingsUpdate = {}
  if (changed.has('providerDefault')) update.provider_default = current.providerDefault
  if (changed.has('cloudEndpoint')) cloud.endpoint = norm(current.cloudEndpoint)
  if (changed.has('cloudModel')) cloud.model = norm(current.cloudModel)
  if (changed.has('cloudFileTransfer')) cloud.file_transfer = current.cloudFileTransfer
  if (norm(keys.cloud)) cloud.api_key = norm(keys.cloud)
  if (changed.has('llmProvider')) llm.provider = current.llmProvider
  if (changed.has('llmEndpoint')) llm.endpoint = norm(current.llmEndpoint)
  if (changed.has('llmModel')) llm.model = norm(current.llmModel)
  if (norm(keys.llm)) llm.api_key = norm(keys.llm)
  if (Object.keys(cloud).length) update.cloud = cloud
  if (Object.keys(llm).length) update.llm = llm
  return Object.keys(update).length ? update : null
}

/**
 * Per-field sync after the server data changed: a field the user has not touched (still equal to
 * the previous baseline) takes the new server value; an edited one keeps the input unless it is
 * in `force` (just saved or reset). Returns the new form values.
 */
export function syncValues(
  current: OnlineValues,
  previous: OnlineValues | null,
  next: OnlineValues,
  force: readonly OnlineField[] | 'all' = [],
): OnlineValues {
  const result = { ...current }
  for (const f of ONLINE_FIELDS) {
    const untouched = previous === null || norm(current[f]) === norm(previous[f])
    if (untouched || force === 'all' || force.includes(f)) {
      ;(result as Record<OnlineField, string>)[f] = next[f]
    }
  }
  return result
}

/** An OpenAI-compatible base URL: http(s) with a host. */
export function isHttpUrl(value: string): boolean {
  try {
    const url = new URL(value.trim())
    return (url.protocol === 'http:' || url.protocol === 'https:') && Boolean(url.host)
  } catch {
    return false
  }
}

/** Host of the server the LLM pass sends transcript text to, for the privacy note. */
export function llmTargetHost(values: OnlineValues, cloudEndpoint: string): string | null {
  const raw = norm(values.llmEndpoint) || (values.llmProvider === 'dashscope' ? cloudEndpoint : '')
  if (!raw) return null
  try {
    return new URL(raw).host || null
  } catch {
    return null
  }
}

/**
 * With provider DashScope an unset LLM endpoint / key inherits the cloud recognition one: the
 * backend reports source `default` and the effective (cloud) value, e.g. the cloud key's mask.
 * The cloud key is never sent to an OpenAI-compatible server.
 */
export function inheritsFromCloud(
  server: Pick<SettingsOut, 'llm' | 'sources' | 'cloud'> | null | undefined,
  key: 'llm.endpoint' | 'llm.api_key',
): boolean {
  return server?.llm?.provider === 'dashscope' && sourceOf(server, key) === 'default'
}

/**
 * Saving `current` moves the LLM to another OpenAI-compatible host (provider switched to openai,
 * or the endpoint changed while openai): the backend then drops a stored LLM key unless the same
 * request carries a new one, so a key is never sent to a host it was not entered for.
 */
export function dropsLlmKey(baseline: OnlineValues, current: OnlineValues): boolean {
  if (current.llmProvider !== 'openai') return false
  return (
    baseline.llmProvider !== 'openai' || norm(baseline.llmEndpoint) !== norm(current.llmEndpoint)
  )
}

/** Note the backend adds to a successful test when the server has no JSON mode. */
const NO_JSON_MODE = /no JSON mode/i

/** How to show a `test-llm` result: a success without JSON mode is a warning-styled success. */
export function llmTestKind(result: {
  ok: boolean
  message: string
}): 'success' | 'warning' | 'error' {
  if (!result.ok) return 'error'
  return NO_JSON_MODE.test(result.message) ? 'warning' : 'success'
}
