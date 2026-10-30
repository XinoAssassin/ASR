<script setup lang="ts">
/**
 * "在线服务": the default provider, cloud recognition (DashScope) and the LLM used for summaries
 * and L3 proofreading suggestions. Every field shows where its effective value comes from
 * (`SettingsOut.sources`) and, when it was set here, a reset that sends `""` (fall back to the
 * config file / env / default). Save sends only the changed fields; keys are write-only.
 */
import {
  NAlert,
  NButton,
  NCard,
  NDivider,
  NForm,
  NFormItem,
  NInput,
  NRadio,
  NRadioGroup,
  NSelect,
  NSpace,
  NTag,
  NText,
  useMessage,
} from 'naive-ui'
import { computed, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import type { FileTransfer, LlmProviderName, ProviderName, SettingSourceKey } from '@/api'
import SourceTag from '@/components/SourceTag.vue'
import { useApiErrors } from '@/composables/useApiErrors'
import { useSettingsStore } from '@/stores/settings'
import {
  buildUpdate,
  changedFields,
  dropsLlmKey,
  FIELD_SOURCE,
  isHttpUrl,
  inheritsFromCloud,
  llmTargetHost,
  llmTestKind,
  resetUpdate,
  serverValues,
  sourceOf,
  syncValues,
  type OnlineField,
  type OnlineValues,
} from '@/utils/onlineSettings'

const { t } = useI18n()
const message = useMessage()
const { describe, notify } = useApiErrors()
const settings = useSettingsStore()

const ENDPOINTS = ['https://dashscope.aliyuncs.com', 'https://cn-beijing.maas.qianwenaiapi.com']
const WORKSPACE_RE = /^https:\/\/([a-z0-9][a-z0-9-]{0,62})\.cn-beijing\.maas\.aliyuncs\.com\/?$/i
const DEFAULT_MODEL = 'qwen-audio-3.1-asr-flash-filetrans'
const OPENAI_EXAMPLE = 'http://127.0.0.1:8080/v1'

const form = reactive({
  providerDefault: 'cloud' as ProviderName,
  endpointChoice: ENDPOINTS[0]!,
  workspace: '',
  cloudModel: '',
  cloudFileTransfer: 'temp_upload' as FileTransfer,
  cloudKey: '',
  llmProvider: 'dashscope' as LlmProviderName,
  llmEndpoint: '',
  llmModel: '',
  llmKey: '',
})

const endpointOptions = computed(() => [
  ...ENDPOINTS.map((url) => ({ label: url, value: url })),
  { label: t('settings.cloud.endpointCustom'), value: 'custom' },
])

const workspaceValid = computed(() => /^[a-z0-9][a-z0-9-]{0,62}$/i.test(form.workspace.trim()))

const resolvedEndpoint = computed(() =>
  form.endpointChoice === 'custom'
    ? `https://${form.workspace.trim()}.cn-beijing.maas.aliyuncs.com`
    : form.endpointChoice,
)

const values = computed<OnlineValues>(() => ({
  providerDefault: form.providerDefault,
  cloudEndpoint: resolvedEndpoint.value,
  cloudModel: form.cloudModel,
  cloudFileTransfer: form.cloudFileTransfer,
  llmProvider: form.llmProvider,
  llmEndpoint: form.llmEndpoint,
  llmModel: form.llmModel,
}))

function setCloudEndpoint(endpoint: string): void {
  const match = WORKSPACE_RE.exec(endpoint)
  if (match) {
    form.endpointChoice = 'custom'
    form.workspace = match[1]!
  } else {
    form.endpointChoice = ENDPOINTS.includes(endpoint) ? endpoint : ENDPOINTS[0]!
  }
}

function apply(next: OnlineValues): void {
  form.providerDefault = next.providerDefault
  // Only re-parse the endpoint when it changed: keeps a half-typed workspace choice intact.
  if (next.cloudEndpoint !== resolvedEndpoint.value) setCloudEndpoint(next.cloudEndpoint)
  form.cloudModel = next.cloudModel
  form.cloudFileTransfer = next.cloudFileTransfer
  form.llmProvider = next.llmProvider
  form.llmEndpoint = next.llmEndpoint
  form.llmModel = next.llmModel
}

/** What the server last reported; fields still equal to it are "untouched". */
const baseline = ref<OnlineValues | null>(null)

function syncFromServer(force: readonly OnlineField[] | 'all' = []): void {
  const s = settings.server
  if (!s) {
    // Another backend (or not loaded yet): start over, typed keys belong to the previous one.
    baseline.value = null
    form.cloudKey = ''
    form.llmKey = ''
    return
  }
  const next = serverValues(s)
  apply(syncValues(values.value, baseline.value, next, force))
  baseline.value = next
}

watch(() => settings.server, () => syncFromServer(), { immediate: true })

const changed = computed(() => (baseline.value ? changedFields(baseline.value, values.value) : []))
const dirty = computed(
  () => changed.value.length > 0 || Boolean(form.cloudKey.trim() || form.llmKey.trim()),
)

const server = computed(() => settings.server)
const hasLlm = computed(() => Boolean(server.value?.llm))
const src = (key: SettingSourceKey) => sourceOf(server.value, key)

const llmTouched = computed(
  () => changed.value.some((f) => f.startsWith('llm')) || Boolean(form.llmKey.trim()),
)

const llmEndpointError = computed<string | null>(() => {
  const endpoint = form.llmEndpoint.trim()
  if (form.llmProvider === 'openai' && !endpoint) return t('settings.llm.endpointRequired')
  if (endpoint && !isHttpUrl(endpoint)) return t('settings.llm.endpointInvalid')
  return null
})

const llmEndpointPlaceholder = computed(() =>
  form.llmProvider === 'dashscope'
    ? resolvedEndpoint.value
    : baseline.value?.llmEndpoint || OPENAI_EXAMPLE,
)

/**
 * A provider switch made here empties the endpoint (a DashScope URL is no OpenAI base URL and vice
 * versa); switching back to the saved provider restores the saved endpoint.
 */
function onLlmProvider(provider: LlmProviderName): void {
  if (provider === form.llmProvider) return
  form.llmProvider = provider
  form.llmEndpoint = baseline.value?.llmProvider === provider ? baseline.value.llmEndpoint : ''
}

const keyInherited = computed(() => inheritsFromCloud(server.value, 'llm.api_key'))
const endpointInherited = computed(() => inheritsFromCloud(server.value, 'llm.endpoint'))

const llmTestType = computed(() => (settings.llmTest ? llmTestKind(settings.llmTest) : 'success'))
const llmTestTitle = computed(() =>
  llmTestType.value === 'error'
    ? t('settings.llm.testFailed')
    : llmTestType.value === 'warning'
      ? t('settings.llm.testOkNoJson')
      : t('settings.llm.testOk'),
)

/** The backend drops a stored LLM key when the OpenAI host changes without a new key. */
const keyWillDrop = computed(
  () =>
    Boolean(baseline.value) &&
    src('llm.api_key') === 'settings' &&
    !form.llmKey.trim() &&
    dropsLlmKey(baseline.value!, values.value),
)

const llmHost = computed(() => llmTargetHost(values.value, resolvedEndpoint.value))

const configFile = computed(() => server.value?.config_file ?? null)

async function save(): Promise<void> {
  if (!baseline.value) return
  if (form.endpointChoice === 'custom' && !workspaceValid.value) {
    message.error(t('settings.cloud.workspaceInvalid'))
    return
  }
  if (llmTouched.value && llmEndpointError.value) {
    message.error(llmEndpointError.value)
    return
  }
  const update = buildUpdate(baseline.value, values.value, {
    cloud: form.cloudKey,
    llm: hasLlm.value ? form.llmKey : '',
  })
  if (!update) {
    message.info(t('settings.online.nothingChanged'))
    return
  }
  try {
    await settings.save(update)
    form.cloudKey = ''
    form.llmKey = ''
    syncFromServer('all')
    message.success(t('settings.saved'))
  } catch (e) {
    notify(e, t('settings.saveFailed'))
  }
}

async function reset(field: OnlineField): Promise<void> {
  const key = FIELD_SOURCE[field]
  if (!key) return
  try {
    await settings.save(resetUpdate(key))
    syncFromServer([field])
    message.success(t('settings.online.resetDone'))
  } catch (e) {
    notify(e, t('settings.saveFailed'))
  }
}

async function clearKey(section: 'cloud' | 'llm'): Promise<void> {
  try {
    await settings.save(resetUpdate(`${section}.api_key`))
    message.success(t(section === 'cloud' ? 'settings.cloud.keyCleared' : 'settings.llm.keyCleared'))
  } catch (e) {
    notify(e, t('settings.saveFailed'))
  }
}

/** Clear is offered for a key set here; older backends without `sources` for any key that is set. */
function keyClearable(section: 'cloud' | 'llm'): boolean {
  const s = server.value
  if (!s) return false
  const source = src(`${section}.api_key`)
  if (source) return source === 'settings'
  return section === 'cloud' ? s.cloud.api_key_set : false
}

async function runCloudTest(): Promise<void> {
  try {
    await settings.testCloud()
  } catch (e) {
    notify(e, t('settings.cloud.testFailed'))
  }
}

async function runLlmTest(): Promise<void> {
  try {
    await settings.testLlm()
  } catch (e) {
    notify(e, t('settings.llm.testFailed'))
  }
}
</script>

<template>
  <NCard :title="t('settings.online.title')" data-testid="online-card">
    <NAlert v-if="settings.error" type="error" style="margin-bottom: 16px">
      {{ describe(settings.error) }}
    </NAlert>

    <!-- Config file status (path / loaded / error / warnings) -->
    <div v-if="configFile" class="config-line" data-testid="config-file">
      <NText depth="3">{{ t('settings.online.configFile') }}</NText>
      <template v-if="configFile.path">
        <code class="mono" data-testid="config-file-path">{{ configFile.path }}</code>
        <NTag
          size="tiny"
          :bordered="false"
          :type="configFile.loaded ? 'success' : 'warning'"
          data-testid="config-file-loaded"
        >
          {{ configFile.loaded ? t('settings.online.configLoaded') : t('settings.online.configNotLoaded') }}
        </NTag>
      </template>
      <NText v-else depth="3">{{ t('settings.online.configNone') }}</NText>
    </div>
    <NAlert
      v-if="configFile?.error"
      type="error"
      :title="t('settings.online.configError')"
      data-testid="config-file-error"
      style="margin-bottom: 12px"
    >
      {{ configFile.error }}
    </NAlert>
    <NAlert
      v-if="configFile?.warnings.length"
      type="warning"
      :title="t('settings.online.configWarnings')"
      data-testid="config-file-warnings"
      style="margin-bottom: 12px"
    >
      <ul class="warnings">
        <li v-for="(w, i) in configFile.warnings" :key="i">{{ w }}</li>
      </ul>
    </NAlert>

    <NForm label-placement="left" label-width="140" :disabled="!settings.server">
      <NFormItem :label="t('settings.server.defaultProvider')">
        <NRadioGroup v-model:value="form.providerDefault" data-testid="default-provider">
          <NRadio value="cloud">{{ t('provider.cloud') }}</NRadio>
          <NRadio value="local">{{ t('provider.local') }}</NRadio>
        </NRadioGroup>
      </NFormItem>

      <!-- 1. Cloud recognition -------------------------------------------------------------->
      <NDivider title-placement="left" data-testid="online-cloud">
        {{ t('settings.online.cloudTitle') }}
      </NDivider>
      <NAlert type="warning" :title="t('settings.cloud.privacyTitle')" style="margin-bottom: 16px">
        {{ t('settings.cloud.privacy') }}
      </NAlert>
      <NFormItem :label="t('settings.cloud.endpoint')">
        <div class="stack">
          <div class="row">
            <NSelect
              v-model:value="form.endpointChoice"
              :options="endpointOptions"
              data-testid="cloud-endpoint"
              class="input"
            />
            <SourceTag
              field="cloud.endpoint"
              :source="src('cloud.endpoint')"
              :busy="settings.saving"
              @reset="reset('cloudEndpoint')"
            />
          </div>
          <NInput
            v-if="form.endpointChoice === 'custom'"
            v-model:value="form.workspace"
            data-testid="cloud-workspace"
            :status="form.workspace && !workspaceValid ? 'error' : undefined"
            :placeholder="t('settings.cloud.workspacePlaceholder')"
            class="input-col"
          >
            <template #prefix>https://</template>
            <template #suffix>.cn-beijing.maas.aliyuncs.com</template>
          </NInput>
        </div>
      </NFormItem>
      <NFormItem :label="t('settings.cloud.model')">
        <div class="row">
          <NInput
            v-model:value="form.cloudModel"
            data-testid="cloud-model"
            :placeholder="baseline?.cloudModel || DEFAULT_MODEL"
            class="input"
          />
          <SourceTag
            field="cloud.model"
            :source="src('cloud.model')"
            :busy="settings.saving"
            @reset="reset('cloudModel')"
          />
        </div>
      </NFormItem>
      <NFormItem :label="t('settings.cloud.apiKey')">
        <div class="stack">
          <div class="row">
            <NTag v-if="server?.cloud.api_key_set" type="success" size="small" data-testid="cloud-key-state">
              {{ t('settings.cloud.keySet') }}
              <template v-if="server.cloud.api_key_masked"> · {{ server.cloud.api_key_masked }}</template>
            </NTag>
            <NTag v-else type="warning" size="small" data-testid="cloud-key-state">
              {{ t('settings.cloud.keyNotSet') }}
            </NTag>
            <SourceTag field="cloud.api_key" :source="src('cloud.api_key')" :resettable="false" />
            <NButton
              v-if="keyClearable('cloud')"
              size="tiny"
              text
              type="error"
              :disabled="settings.saving"
              data-testid="cloud-key-clear"
              @click="clearKey('cloud')"
            >
              {{ t('settings.cloud.clearKey') }}
            </NButton>
          </div>
          <NInput
            v-model:value="form.cloudKey"
            data-testid="cloud-key-input"
            type="password"
            show-password-on="click"
            :placeholder="t('settings.cloud.keyPlaceholder')"
            :input-props="{ autocomplete: 'new-password' }"
            class="input-col"
          />
          <NText depth="3" class="hint">{{ t('settings.cloud.keyHint') }}</NText>
        </div>
      </NFormItem>
      <NFormItem :label="t('settings.cloud.fileTransfer')">
        <div class="stack">
          <div class="row">
            <NRadioGroup v-model:value="form.cloudFileTransfer" data-testid="cloud-file-transfer">
              <NRadio value="temp_upload">{{ t('settings.cloud.tempUpload') }}</NRadio>
              <NRadio value="public_url">{{ t('settings.cloud.publicUrl') }}</NRadio>
            </NRadioGroup>
            <SourceTag
              field="cloud.file_transfer"
              :source="src('cloud.file_transfer')"
              :busy="settings.saving"
              @reset="reset('cloudFileTransfer')"
            />
          </div>
          <NText v-if="form.cloudFileTransfer === 'temp_upload'" depth="3" class="hint">
            {{ t('settings.cloud.tempUploadCaveat') }}
          </NText>
          <NText v-else depth="3" class="hint">{{ t('settings.cloud.publicUrlHint') }}</NText>
        </div>
      </NFormItem>
      <NFormItem label=" ">
        <NSpace align="center">
          <NButton :loading="settings.testing" data-testid="cloud-test" @click="runCloudTest">
            {{ t('settings.cloud.test') }}
          </NButton>
          <NText depth="3" class="hint">{{ t('settings.cloud.testHint') }}</NText>
        </NSpace>
      </NFormItem>
      <NAlert
        v-if="settings.cloudTest"
        :type="settings.cloudTest.ok ? 'success' : 'error'"
        data-testid="cloud-test-result"
        :title="settings.cloudTest.ok ? t('settings.cloud.testOk') : t('settings.cloud.testFailed')"
        style="margin-bottom: 16px"
      >
        {{ settings.cloudTest.message }}
        <template v-if="settings.cloudTest.max_file_size_mb">
          <br />
          {{ t('settings.cloud.maxFileSize', { mb: settings.cloudTest.max_file_size_mb }) }}
        </template>
      </NAlert>

      <!-- 2. LLM (summary, proofreading suggestions) ---------------------------------------->
      <NDivider title-placement="left" data-testid="online-llm">
        {{ t('settings.online.llmTitle') }}
      </NDivider>
      <NText v-if="server && !hasLlm" depth="3" data-testid="llm-unsupported">
        {{ t('settings.llm.unsupported') }}
      </NText>
      <template v-else>
        <NAlert
          type="warning"
          :title="t('settings.llm.privacyTitle')"
          data-testid="llm-privacy"
          style="margin-bottom: 16px"
        >
          {{ llmHost ? t('settings.llm.privacy', { host: llmHost }) : t('settings.llm.privacyUnknown') }}
        </NAlert>
        <NFormItem :label="t('settings.llm.provider')">
          <div class="row">
            <NRadioGroup
              :value="form.llmProvider"
              data-testid="llm-provider"
              @update:value="onLlmProvider"
            >
              <NRadio value="dashscope">{{ t('settings.llm.providerDashscope') }}</NRadio>
              <NRadio value="openai">{{ t('settings.llm.providerOpenai') }}</NRadio>
            </NRadioGroup>
            <SourceTag
              field="llm.provider"
              :source="src('llm.provider')"
              :busy="settings.saving"
              @reset="reset('llmProvider')"
            />
          </div>
        </NFormItem>
        <NFormItem :label="t('settings.llm.endpoint')">
          <div class="stack">
            <div class="row">
              <NInput
                v-model:value="form.llmEndpoint"
                data-testid="llm-endpoint"
                :placeholder="llmEndpointPlaceholder"
                :status="form.llmEndpoint.trim() && llmEndpointError ? 'error' : undefined"
                clearable
                class="input"
              />
              <SourceTag
                field="llm.endpoint"
                :source="src('llm.endpoint')"
                :inherited="endpointInherited"
                :busy="settings.saving"
                @reset="reset('llmEndpoint')"
              />
            </div>
            <NText depth="3" class="hint" data-testid="llm-endpoint-hint">
              {{
                form.llmProvider === 'dashscope'
                  ? t('settings.llm.endpointHintDashscope')
                  : t('settings.llm.endpointHintOpenai')
              }}
            </NText>
          </div>
        </NFormItem>
        <NFormItem :label="t('settings.llm.model')">
          <div class="row">
            <NInput
              v-model:value="form.llmModel"
              data-testid="llm-model"
              :placeholder="baseline?.llmModel || ''"
              class="input"
            />
            <SourceTag
              field="llm.model"
              :source="src('llm.model')"
              :busy="settings.saving"
              @reset="reset('llmModel')"
            />
          </div>
        </NFormItem>
        <NFormItem :label="t('settings.llm.apiKey')">
          <div class="stack">
            <div class="row">
              <NTag
                v-if="server?.llm?.api_key_set"
                type="success"
                size="small"
                data-testid="llm-key-state"
              >
                {{ t('settings.cloud.keySet') }}
                <template v-if="server.llm.api_key_masked"> · {{ server.llm.api_key_masked }}</template>
              </NTag>
              <NTag
                v-else
                :type="server?.llm?.provider === 'openai' ? 'default' : 'warning'"
                size="small"
                data-testid="llm-key-state"
              >
                {{
                  server?.llm?.provider === 'openai'
                    ? t('settings.llm.keyOptional')
                    : t('settings.cloud.keyNotSet')
                }}
              </NTag>
              <SourceTag
                field="llm.api_key"
                :source="src('llm.api_key')"
                :inherited="keyInherited"
                :resettable="false"
              />
              <NButton
                v-if="keyClearable('llm')"
                size="tiny"
                text
                type="error"
                :disabled="settings.saving"
                data-testid="llm-key-clear"
                @click="clearKey('llm')"
              >
                {{ t('settings.cloud.clearKey') }}
              </NButton>
            </div>
            <NInput
              v-model:value="form.llmKey"
              data-testid="llm-key-input"
              type="password"
              show-password-on="click"
              :placeholder="t('settings.cloud.keyPlaceholder')"
              :input-props="{ autocomplete: 'new-password' }"
              class="input-col"
            />
            <NText depth="3" class="hint">
              {{
                form.llmProvider === 'dashscope'
                  ? t('settings.llm.keyHintDashscope')
                  : t('settings.llm.keyHintOpenai')
              }}
            </NText>
            <NText v-if="keyWillDrop" type="warning" class="hint" data-testid="llm-key-drop">
              {{ t('settings.llm.keyWillDrop') }}
            </NText>
          </div>
        </NFormItem>
        <NFormItem label=" ">
          <NSpace align="center">
            <NButton :loading="settings.testingLlm" data-testid="llm-test" @click="runLlmTest">
              {{ t('settings.llm.test') }}
            </NButton>
            <NText depth="3" class="hint">{{ t('settings.cloud.testHint') }}</NText>
          </NSpace>
        </NFormItem>
        <NAlert
          v-if="settings.llmTest"
          :type="llmTestType"
          data-testid="llm-test-result"
          :data-kind="llmTestType"
          :title="llmTestTitle"
          style="margin-bottom: 16px"
        >
          {{ settings.llmTest.message }}
          <template v-if="settings.llmTest.model">
            <br />
            {{ t('settings.llm.testModel', { model: settings.llmTest.model }) }}
          </template>
        </NAlert>
      </template>

      <NDivider />
      <NFormItem label=" ">
        <NSpace align="center">
          <NButton
            type="primary"
            :loading="settings.saving"
            :disabled="!dirty"
            data-testid="settings-save"
            @click="save"
          >
            {{ t('common.save') }}
          </NButton>
        </NSpace>
      </NFormItem>
    </NForm>
  </NCard>
</template>

<style scoped>
.config-line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-bottom: 12px;
  font-size: 12px;
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  word-break: break-all;
}

.warnings {
  margin: 0;
  padding-left: 18px;
}

.stack {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
}

.row {
  display: flex;
  width: 100%;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.input {
  flex: 1 1 260px;
  max-width: 420px;
}

.input-col {
  max-width: 420px;
}

.hint {
  font-size: 12px;
}
</style>
