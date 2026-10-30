<script setup lang="ts">
/**
 * Settings → SiYuan (D61): endpoint, write-only API token (shown masked, like the DashScope key),
 * default notebook (listed once a token is set / after a successful test), path template, Save
 * (`PUT /api/siyuan/settings`) and Test (`POST /api/siyuan/test`). Changing the endpoint without a
 * new token clears the stored token on the server; the card says so before saving.
 */
import {
  NAlert,
  NButton,
  NCard,
  NForm,
  NFormItem,
  NInput,
  NSelect,
  NSpace,
  NTag,
  NText,
  useMessage,
} from 'naive-ui'
import { computed, onMounted, reactive, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import type { SiYuanSettingsUpdate } from '@/api'
import { useApiErrors } from '@/composables/useApiErrors'
import { useSiyuanStore } from '@/stores/siyuan'
import { createFormSync } from '@/views/formSync'
import { PATH_FIELDS, pathTemplateProblem } from '@/utils/siyuanPath'

const { t } = useI18n()
const message = useMessage()
const { describe, notify } = useApiErrors()
const siyuan = useSiyuanStore()

const DEFAULT_ENDPOINT = 'http://127.0.0.1:6806'

const form = reactive({ endpoint: '', token: '', notebook: '', pathTemplate: '' })

function fill(): boolean {
  const s = siyuan.settings
  if (!s) return false
  form.endpoint = s.endpoint
  form.token = ''
  form.notebook = s.notebook ?? ''
  form.pathTemplate = s.path_template
  return true
}

const sync = createFormSync(form, fill)
watch(
  () => siyuan.settings,
  () => sync.sync(),
  { immediate: true },
)

function sameEndpoint(a: string, b: string): boolean {
  const norm = (u: string) => u.trim().replace(/\/+$/, '') || DEFAULT_ENDPOINT
  return norm(a) === norm(b)
}

const endpointChanged = computed(
  () => !!siyuan.settings && !sameEndpoint(form.endpoint, siyuan.settings.endpoint),
)
/** The stored token would be dropped by this save. */
const tokenWillClear = computed(
  () => endpointChanged.value && !!siyuan.settings?.token_set && !form.token.trim(),
)

const fieldLabels = computed(() =>
  (siyuan.settings?.path_fields?.length ? siyuan.settings.path_fields : [...PATH_FIELDS]).map(
    (f) => `{${f}}`,
  ),
)

const templateProblem = computed(() => {
  const p = pathTemplateProblem(form.pathTemplate)
  if (!p) return null
  return t(`siyuan.settings.pathTemplateProblem.${p.reason}`, {
    name: p.reason === 'placeholder' ? `{${p.name}}` : '',
  })
})

const notebookOptions = computed(() => {
  const options = [
    { label: t('siyuan.settings.notebookNone'), value: '', disabled: false },
    ...siyuan.notebooks.map((nb) => ({
      label: nb.closed ? t('siyuan.notebookClosed', { name: nb.name }) : nb.name,
      value: nb.id,
      disabled: nb.closed,
    })),
  ]
  // Keep the saved notebook visible even when the list is not loaded (or it was deleted).
  if (form.notebook && !options.some((o) => o.value === form.notebook)) {
    options.push({ label: form.notebook, value: form.notebook, disabled: false })
  }
  return options
})

async function save(): Promise<void> {
  if (templateProblem.value) {
    message.error(templateProblem.value)
    return
  }
  const s = siyuan.settings
  const update: SiYuanSettingsUpdate = {}
  if (!s || !sameEndpoint(form.endpoint, s.endpoint)) update.endpoint = form.endpoint.trim()
  if (form.token.trim()) update.token = form.token.trim()
  if (!s || form.notebook !== (s.notebook ?? '')) update.notebook = form.notebook
  if (!s || form.pathTemplate.trim() !== s.path_template) update.path_template = form.pathTemplate.trim()
  try {
    await siyuan.save(update)
    sync.refill()
    message.success(t('settings.saved'))
  } catch (e) {
    notify(e, t('settings.saveFailed'))
  }
}

async function clearToken(): Promise<void> {
  try {
    await siyuan.save({ token: '' })
    message.success(t('siyuan.settings.tokenCleared'))
  } catch (e) {
    notify(e, t('settings.saveFailed'))
  }
}

async function runTest(): Promise<void> {
  try {
    await siyuan.test()
  } catch (e) {
    notify(e, t('siyuan.settings.testFailed'))
  }
}

onMounted(async () => {
  await siyuan.load()
  if (siyuan.settings?.token_set) void siyuan.loadNotebooks()
})
</script>

<template>
  <NCard id="siyuan" :title="t('siyuan.settings.title')" data-testid="siyuan-settings">
    <NAlert v-if="siyuan.error" type="error" class="gap">{{ describe(siyuan.error) }}</NAlert>
    <NText depth="3" class="intro">{{ t('siyuan.settings.intro') }}</NText>
    <NForm label-placement="left" label-width="140" :disabled="!siyuan.settings">
      <NFormItem :label="t('siyuan.settings.endpoint')">
        <NSpace vertical style="width: 100%">
          <NInput
            v-model:value="form.endpoint"
            :placeholder="t('siyuan.settings.endpointPlaceholder')"
            data-testid="siyuan-endpoint"
            style="max-width: 420px"
          />
          <NText depth="3" class="small">{{ t('siyuan.settings.endpointHint') }}</NText>
          <NAlert
            v-if="tokenWillClear"
            type="warning"
            :show-icon="false"
            data-testid="siyuan-endpoint-warning"
          >
            {{ t('siyuan.settings.endpointChangeWarning') }}
          </NAlert>
        </NSpace>
      </NFormItem>
      <NFormItem :label="t('siyuan.settings.token')">
        <NSpace vertical style="width: 100%">
          <NSpace align="center">
            <NTag v-if="siyuan.settings?.token_set" type="success" size="small" data-testid="siyuan-token-state">
              {{ t('siyuan.settings.tokenSet') }}
              <template v-if="siyuan.settings.token_masked"> · {{ siyuan.settings.token_masked }}</template>
            </NTag>
            <NTag v-else type="warning" size="small" data-testid="siyuan-token-state">
              {{ t('siyuan.settings.tokenNotSet') }}
            </NTag>
            <NButton
              v-if="siyuan.settings?.token_set"
              size="small"
              quaternary
              type="error"
              data-testid="siyuan-token-clear"
              @click="clearToken"
            >
              {{ t('siyuan.settings.clearToken') }}
            </NButton>
          </NSpace>
          <NInput
            v-model:value="form.token"
            type="password"
            show-password-on="click"
            :placeholder="t('siyuan.settings.tokenPlaceholder')"
            :input-props="{ autocomplete: 'new-password' }"
            data-testid="siyuan-token"
            style="max-width: 420px"
          />
          <NText depth="3" class="small">{{ t('siyuan.settings.tokenHint') }}</NText>
        </NSpace>
      </NFormItem>
      <NFormItem :label="t('siyuan.settings.notebook')">
        <NSpace vertical style="width: 100%">
          <NSelect
            v-model:value="form.notebook"
            :options="notebookOptions"
            :loading="siyuan.notebooksLoading"
            :disabled="!siyuan.settings?.token_set"
            data-testid="siyuan-default-notebook"
            style="max-width: 420px"
          />
          <NText v-if="!siyuan.settings?.token_set" depth="3" class="small">
            {{ t('siyuan.settings.notebookLoadHint') }}
          </NText>
          <NText v-else-if="siyuan.notebooksError" type="error" class="small" data-testid="siyuan-notebooks-error">
            {{ t('common.labelDetail', { label: t('siyuan.notebooksFailed'), detail: describe(siyuan.notebooksError) }) }}
          </NText>
        </NSpace>
      </NFormItem>
      <NFormItem
        :label="t('siyuan.settings.pathTemplate')"
        :validation-status="templateProblem ? 'error' : undefined"
        :feedback="templateProblem ?? undefined"
      >
        <NSpace vertical style="width: 100%">
          <NInput
            v-model:value="form.pathTemplate"
            data-testid="siyuan-path-template"
            style="max-width: 420px"
          />
          <NText depth="3" class="small" data-testid="siyuan-path-fields">
            {{ t('siyuan.settings.pathTemplateHint') }}
            <code v-for="f in fieldLabels" :key="f" class="field">{{ f }}</code>
          </NText>
        </NSpace>
      </NFormItem>
      <NFormItem label=" ">
        <NSpace align="center">
          <NButton type="primary" :loading="siyuan.saving" data-testid="siyuan-save" @click="save">
            {{ t('siyuan.settings.save') }}
          </NButton>
          <NButton
            :loading="siyuan.testing"
            :disabled="!siyuan.settings?.token_set"
            data-testid="siyuan-test"
            @click="runTest"
          >
            {{ t('siyuan.settings.test') }}
          </NButton>
          <NText depth="3">{{ t('siyuan.settings.testHint') }}</NText>
        </NSpace>
      </NFormItem>
    </NForm>
    <NAlert
      v-if="siyuan.testResult"
      :type="siyuan.testResult.ok ? 'success' : 'error'"
      :title="siyuan.testResult.ok ? t('siyuan.settings.testOk') : t('siyuan.settings.testFailed')"
      data-testid="siyuan-test-result"
    >
      <div v-if="siyuan.testResult.ok && siyuan.testResult.version">
        {{ t('siyuan.settings.testVersion', { version: siyuan.testResult.version, n: siyuan.testResult.notebooks ?? 0 }) }}
      </div>
      <div>{{ siyuan.testResult.message }}</div>
    </NAlert>
  </NCard>
</template>

<style scoped>
.gap {
  margin-bottom: 16px;
}

.intro {
  display: block;
  margin-bottom: 16px;
}

.small {
  font-size: 12px;
}

.field {
  margin-left: 6px;
}
</style>
