<script setup lang="ts">
import {
  NAlert,
  NButton,
  NCard,
  NCheckbox,
  NDataTable,
  NDescriptions,
  NDescriptionsItem,
  NForm,
  NFormItem,
  NInput,
  NRadio,
  NRadioGroup,
  NSelect,
  NSpace,
  NTag,
  NText,
  type DataTableColumns,
  useMessage,
} from 'naive-ui'
import { computed, h, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import {
  downloadLocalModel,
  getDefaultBackendUrl,
  listLocalModels,
  probeHealth,
  type FileTransfer,
  type HealthOut,
  type LocalModelOut,
  type ProviderName,
  type SettingsUpdate,
} from '@/api'
import DesktopSettingsCard from '@/components/recorder/DesktopSettingsCard.vue'
import SidecarCard from '@/components/recorder/SidecarCard.vue'
import { useApiErrors } from '@/composables/useApiErrors'
import { isDesktop } from '@/platform'
import { useHealthStore } from '@/stores/health'
import { useRecorderStore } from '@/stores/recorder'
import { useSettingsStore } from '@/stores/settings'

const { t } = useI18n()
const message = useMessage()
const { describe, notify } = useApiErrors()
const health = useHealthStore()
const settings = useSettingsStore()
const desktop = isDesktop()
const recorder = useRecorderStore()

/** Desktop: the browser connection settings are the source of truth; hand them to Rust too. */
async function pushToDesktop(): Promise<void> {
  if (!desktop) return
  try {
    await recorder.syncBackend()
  } catch (e) {
    message.error(`${t('recorder.settings.syncFailed')}：${(e as Error).message}`)
  }
}

// --- browser-local connection settings --------------------------------------------------------

const urlInput = ref(settings.backendUrl)
// Desktop "use this backend" (local sidecar) changes the override from outside this form.
watch(
  () => settings.backendUrl,
  (url) => (urlInput.value = url),
)
const keyInput = ref(settings.apiKey)
const probing = ref(false)
const probeResult = ref<{ ok: true; info: HealthOut } | { ok: false; text: string } | null>(null)
const defaultUrl = getDefaultBackendUrl()

async function testBackendUrl(): Promise<void> {
  probing.value = true
  probeResult.value = null
  try {
    const info = await probeHealth(urlInput.value.trim() || defaultUrl)
    probeResult.value = { ok: true, info }
  } catch (e) {
    probeResult.value = { ok: false, text: describe(e) }
  } finally {
    probing.value = false
  }
}

function saveBackendUrl(): void {
  settings.setBackendUrl(urlInput.value)
  urlInput.value = settings.backendUrl
  message.success(t('settings.saved'))
  void settings.load()
  void pushToDesktop()
}

function resetBackendUrl(): void {
  urlInput.value = ''
  saveBackendUrl()
}

function saveApiKey(): void {
  settings.setApiKey(keyInput.value)
  message.success(t('settings.saved'))
  void health.check()
  void settings.load()
  void loadLocalModels() // the list was empty (401) before the key was set
  void pushToDesktop()
}

// --- server-side settings ---------------------------------------------------------------------

const ENDPOINTS = ['https://dashscope.aliyuncs.com', 'https://cn-beijing.maas.qianwenaiapi.com']
const WORKSPACE_RE = /^https:\/\/([a-z0-9][a-z0-9-]{0,62})\.cn-beijing\.maas\.aliyuncs\.com\/?$/i
const DEFAULT_MODEL = 'qwen-audio-3.1-asr-flash-filetrans'

const form = reactive({
  provider: 'cloud' as ProviderName,
  endpointChoice: ENDPOINTS[0]!,
  workspace: '',
  model: '',
  fileTransfer: 'temp_upload' as FileTransfer,
  newKey: '',
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

function fillForm(): void {
  const s = settings.server
  if (!s) return
  form.provider = s.provider_default
  form.model = s.cloud.model
  form.fileTransfer = s.cloud.file_transfer
  form.newKey = ''
  const match = WORKSPACE_RE.exec(s.cloud.endpoint)
  if (match) {
    form.endpointChoice = 'custom'
    form.workspace = match[1]!
  } else {
    form.endpointChoice = ENDPOINTS.includes(s.cloud.endpoint) ? s.cloud.endpoint : ENDPOINTS[0]!
  }
}

watch(() => settings.server, fillForm, { immediate: true })

async function saveServer(): Promise<void> {
  if (form.endpointChoice === 'custom' && !workspaceValid.value) {
    message.error(t('settings.cloud.workspaceInvalid'))
    return
  }
  const update: SettingsUpdate = {
    provider_default: form.provider,
    cloud: {
      endpoint: resolvedEndpoint.value,
      model: form.model.trim() || DEFAULT_MODEL,
      file_transfer: form.fileTransfer,
    },
  }
  if (form.newKey.trim()) update.cloud!.api_key = form.newKey.trim()
  try {
    await settings.save(update)
    message.success(t('settings.saved'))
  } catch (e) {
    notify(e, t('settings.saveFailed'))
  }
}

async function clearKey(): Promise<void> {
  try {
    await settings.save({ cloud: { api_key: '' } })
    message.success(t('settings.cloud.keyCleared'))
  } catch (e) {
    notify(e, t('settings.saveFailed'))
  }
}

async function runCloudTest(): Promise<void> {
  try {
    await settings.testCloud()
  } catch (e) {
    notify(e, t('settings.cloud.testFailed'))
  }
}

// --- local models (status only; management in Settings arrives with M6) ----------------------

const localModels = ref<LocalModelOut[]>([])
const localModelsError = ref<string | null>(null)
const useMirror = ref(true)
const localForm = reactive({ asrModel: '', device: 'cpu' as 'cpu' | 'cuda' })
let pollTimer: ReturnType<typeof setInterval> | null = null

const anyDownloading = computed(() => localModels.value.some((m) => m.status === 'downloading'))
const asrModelOptions = computed(() => [
  { label: t('settings.local.auto'), value: '' },
  ...localModels.value
    .filter((m) => m.kind === 'asr')
    .map((m) => {
      const notes = [
        isWhisper(m.id) && t('settings.local.englishOnly'),
        !m.downloaded && t('settings.local.notDownloaded'),
      ].filter(Boolean)
      return { label: notes.length ? `${m.id}（${notes.join('，')}）` : m.id, value: m.id }
    }),
])

/** faster-whisper engines (M11): English only, need the backend's `whisper` extra. */
function isWhisper(id: string): boolean {
  return id.startsWith('faster-whisper')
}

watch(
  () => settings.server,
  (s) => {
    if (!s) return
    localForm.asrModel = s.local.asr_model ?? ''
    localForm.device = s.local.device
  },
  { immediate: true },
)

const STATUS_TYPE = {
  missing: 'default',
  downloading: 'info',
  downloaded: 'success',
  failed: 'error',
} as const

const modelColumns = computed<DataTableColumns<LocalModelOut>>(() => [
  { title: t('settings.local.modelKey'), key: 'id' },
  {
    title: t('settings.local.modelName'),
    key: 'name',
    render: (row) =>
      isWhisper(row.id) ? `${row.name}（${t('settings.local.whisperNote')}）` : row.name,
  },
  { title: t('settings.local.kind'), key: 'kind' },
  {
    title: t('settings.local.size'),
    key: 'size_mb',
    render: (row) => (row.size_mb != null ? `${row.size_mb} MB` : '—'),
  },
  {
    title: t('settings.local.status'),
    key: 'status',
    render: (row) =>
      h(
        NTag,
        { type: STATUS_TYPE[row.status ?? 'missing'], size: 'small', title: row.error ?? '' },
        () => t(`settings.local.statuses.${row.status ?? 'missing'}`),
      ),
  },
  {
    title: '',
    key: 'actions',
    render: (row) =>
      h(
        NButton,
        {
          size: 'small',
          disabled: row.downloaded || anyDownloading.value,
          loading: row.status === 'downloading',
          onClick: () => void startDownload(row.id),
          'data-testid': `download-${row.id}`,
        },
        () => t('settings.local.download'),
      ),
  },
])

function schedulePoll(): void {
  if (anyDownloading.value && pollTimer === null) {
    pollTimer = setInterval(() => void loadLocalModels(), 3000)
  } else if (!anyDownloading.value && pollTimer !== null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function startDownload(key: string): Promise<void> {
  try {
    await downloadLocalModel(key, useMirror.value)
    await loadLocalModels()
  } catch (e) {
    notify(e, t('settings.local.downloadFailed'))
  }
}

async function saveLocal(): Promise<void> {
  try {
    await settings.save({ local: { asr_model: localForm.asrModel, device: localForm.device } })
    message.success(t('settings.saved'))
  } catch (e) {
    notify(e, t('settings.saveFailed'))
  }
}

onBeforeUnmount(() => {
  if (pollTimer !== null) clearInterval(pollTimer)
})

async function loadLocalModels(): Promise<void> {
  try {
    localModels.value = await listLocalModels()
    localModelsError.value = null
    schedulePoll()
  } catch (e) {
    localModelsError.value = describe(e)
  }
}

onMounted(() => {
  void settings.load()
  void loadLocalModels()
})
</script>

<template>
  <NSpace vertical size="large" class="settings">
    <!-- Connection (stored in this browser) -->
    <NCard :title="t('settings.connection.title')">
      <NForm label-placement="left" label-width="140">
        <NFormItem :label="t('settings.connection.backendUrl')">
          <NSpace vertical style="width: 100%">
            <NSpace :wrap="false">
              <NInput
                v-model:value="urlInput"
                data-testid="backend-url-input"
                :placeholder="defaultUrl || t('health.sameOrigin')"
                clearable
                style="min-width: 320px"
              />
              <NButton :loading="probing" data-testid="backend-url-test" @click="testBackendUrl">
                {{ t('settings.connection.test') }}
              </NButton>
              <NButton type="primary" data-testid="backend-url-save" @click="saveBackendUrl">
                {{ t('common.save') }}
              </NButton>
              <NButton v-if="settings.backendUrl" quaternary @click="resetBackendUrl">
                {{ t('settings.connection.reset') }}
              </NButton>
            </NSpace>
            <NText depth="3">{{ t('settings.connection.backendUrlHint') }}</NText>
            <NAlert v-if="probeResult?.ok" type="success" :show-icon="true">
              {{ t('settings.connection.testOk', { version: probeResult.info.version }) }}
            </NAlert>
            <NAlert v-else-if="probeResult" type="error">{{ probeResult.text }}</NAlert>
          </NSpace>
        </NFormItem>
        <NFormItem :label="t('settings.connection.apiKey')">
          <NSpace vertical style="width: 100%">
            <NSpace :wrap="false">
              <NInput
                v-model:value="keyInput"
                data-testid="api-key-input"
                type="password"
                show-password-on="click"
                :placeholder="t('settings.connection.apiKeyPlaceholder')"
                :input-props="{ autocomplete: 'off' }"
                style="min-width: 320px"
              />
              <NButton type="primary" data-testid="api-key-save" @click="saveApiKey">
                {{ t('common.save') }}
              </NButton>
            </NSpace>
            <NText depth="3">{{ t('settings.connection.apiKeyHint') }}</NText>
          </NSpace>
        </NFormItem>
      </NForm>
      <NDescriptions v-if="health.info" :column="2" bordered size="small" label-placement="left">
        <NDescriptionsItem :label="t('health.title')">
          <NTag type="success" size="small">{{ t('health.connected') }}</NTag>
        </NDescriptionsItem>
        <NDescriptionsItem :label="t('health.version')">{{ health.info.version }}</NDescriptionsItem>
        <NDescriptionsItem :label="t('health.decoder')">{{ health.info.decoder }}</NDescriptionsItem>
        <NDescriptionsItem :label="t('health.ffmpeg')">
          {{ health.info.ffmpeg_version ?? '—' }}
        </NDescriptionsItem>
        <NDescriptionsItem :label="t('health.apiKeyRequired')">
          {{ health.info.api_key_required ? t('common.yes') : t('common.no') }}
        </NDescriptionsItem>
      </NDescriptions>
      <NAlert v-else-if="health.state === 'error'" type="error">
        {{ t('health.error') }} — {{ health.error }}
      </NAlert>
    </NCard>

    <!-- Desktop app only: what the Rust core uses for background uploads -->
    <DesktopSettingsCard v-if="desktop" />
    <SidecarCard v-if="desktop" />

    <!-- Server-side settings -->
    <NCard :title="t('settings.server.title')">
      <NAlert v-if="settings.error" type="error" style="margin-bottom: 16px">
        {{ describe(settings.error) }}
      </NAlert>
      <NAlert type="warning" :title="t('settings.cloud.privacyTitle')" style="margin-bottom: 16px">
        {{ t('settings.cloud.privacy') }}
      </NAlert>
      <NForm label-placement="left" label-width="140" :disabled="!settings.server">
        <NFormItem :label="t('settings.server.defaultProvider')">
          <NRadioGroup v-model:value="form.provider" data-testid="default-provider">
            <NRadio value="cloud">{{ t('provider.cloud') }}</NRadio>
            <NRadio value="local">{{ t('provider.local') }}</NRadio>
          </NRadioGroup>
        </NFormItem>
        <NFormItem :label="t('settings.cloud.endpoint')">
          <NSpace vertical style="width: 100%">
            <NSelect
              v-model:value="form.endpointChoice"
              :options="endpointOptions"
              data-testid="cloud-endpoint"
              style="max-width: 420px"
            />
            <NInput
              v-if="form.endpointChoice === 'custom'"
              v-model:value="form.workspace"
              data-testid="cloud-workspace"
              :status="form.workspace && !workspaceValid ? 'error' : undefined"
              :placeholder="t('settings.cloud.workspacePlaceholder')"
              style="max-width: 420px"
            >
              <template #prefix>https://</template>
              <template #suffix>.cn-beijing.maas.aliyuncs.com</template>
            </NInput>
          </NSpace>
        </NFormItem>
        <NFormItem :label="t('settings.cloud.model')">
          <NInput
            v-model:value="form.model"
            data-testid="cloud-model"
            :placeholder="DEFAULT_MODEL"
            style="max-width: 420px"
          />
        </NFormItem>
        <NFormItem :label="t('settings.cloud.apiKey')">
          <NSpace vertical style="width: 100%">
            <NSpace align="center">
              <NTag v-if="settings.server?.cloud.api_key_set" type="success" size="small">
                {{ t('settings.cloud.keySet') }}
                <template v-if="settings.server.cloud.api_key_masked">
                  · {{ settings.server.cloud.api_key_masked }}
                </template>
              </NTag>
              <NTag v-else type="warning" size="small">{{ t('settings.cloud.keyNotSet') }}</NTag>
              <NButton
                v-if="settings.server?.cloud.api_key_set"
                size="small"
                quaternary
                type="error"
                data-testid="cloud-key-clear"
                @click="clearKey"
              >
                {{ t('settings.cloud.clearKey') }}
              </NButton>
            </NSpace>
            <NInput
              v-model:value="form.newKey"
              data-testid="cloud-key-input"
              type="password"
              show-password-on="click"
              :placeholder="t('settings.cloud.keyPlaceholder')"
              :input-props="{ autocomplete: 'new-password' }"
              style="max-width: 420px"
            />
            <NText depth="3">{{ t('settings.cloud.keyHint') }}</NText>
          </NSpace>
        </NFormItem>
        <NFormItem :label="t('settings.cloud.fileTransfer')">
          <NSpace vertical>
            <NRadioGroup v-model:value="form.fileTransfer" data-testid="cloud-file-transfer">
              <NRadio value="temp_upload">{{ t('settings.cloud.tempUpload') }}</NRadio>
              <NRadio value="public_url">{{ t('settings.cloud.publicUrl') }}</NRadio>
            </NRadioGroup>
            <NText v-if="form.fileTransfer === 'temp_upload'" depth="3">
              {{ t('settings.cloud.tempUploadCaveat') }}
            </NText>
            <NText v-else depth="3">{{ t('settings.cloud.publicUrlHint') }}</NText>
          </NSpace>
        </NFormItem>
        <NFormItem label=" ">
          <NSpace>
            <NButton
              type="primary"
              :loading="settings.saving"
              data-testid="settings-save"
              @click="saveServer"
            >
              {{ t('common.save') }}
            </NButton>
            <NButton
              :loading="settings.testing"
              data-testid="cloud-test"
              @click="runCloudTest"
            >
              {{ t('settings.cloud.test') }}
            </NButton>
            <NText depth="3">{{ t('settings.cloud.testHint') }}</NText>
          </NSpace>
        </NFormItem>
      </NForm>
      <NAlert
        v-if="settings.cloudTest"
        :type="settings.cloudTest.ok ? 'success' : 'error'"
        data-testid="cloud-test-result"
        :title="settings.cloudTest.ok ? t('settings.cloud.testOk') : t('settings.cloud.testFailed')"
      >
        {{ settings.cloudTest.message }}
        <template v-if="settings.cloudTest.max_file_size_mb">
          <br />
          {{ t('settings.cloud.maxFileSize', { mb: settings.cloudTest.max_file_size_mb }) }}
        </template>
      </NAlert>
    </NCard>

    <!-- Local models: download state; downloads go through `asr models download` (rule 6) -->
    <NCard :title="t('settings.local.title')">
      <NAlert type="info" style="margin-bottom: 16px">{{ t('settings.local.downloadHint') }}</NAlert>
      <NDescriptions
        v-if="settings.server"
        :column="2"
        bordered
        size="small"
        label-placement="left"
        style="margin-bottom: 16px"
      >
        <NDescriptionsItem :label="t('settings.local.asrModel')">
          {{ settings.server.local.asr_model || t('settings.local.auto') }}
        </NDescriptionsItem>
        <NDescriptionsItem :label="t('settings.local.diarizeModel')">
          {{ settings.server.local.diarize_model ?? '—' }}
        </NDescriptionsItem>
        <NDescriptionsItem :label="t('settings.local.device')">
          {{ settings.server.local.device }}
        </NDescriptionsItem>
        <NDescriptionsItem :label="t('settings.local.cacheDir')">
          {{ settings.server.local.model_cache_dir }}
        </NDescriptionsItem>
      </NDescriptions>
      <NForm label-placement="left" label-width="140" :disabled="!settings.server">
        <NFormItem :label="t('settings.local.asrModel')">
          <NSelect
            v-model:value="localForm.asrModel"
            :options="asrModelOptions"
            data-testid="local-asr-model"
          />
        </NFormItem>
        <NFormItem :label="t('settings.local.device')">
          <NRadioGroup v-model:value="localForm.device" data-testid="local-device">
            <NRadio value="cpu">CPU</NRadio>
            <NRadio value="cuda">CUDA</NRadio>
          </NRadioGroup>
        </NFormItem>
        <NFormItem>
          <NSpace>
            <NButton type="primary" data-testid="local-save" @click="saveLocal">
              {{ t('settings.local.save') }}
            </NButton>
            <NCheckbox v-model:checked="useMirror" data-testid="download-mirror">
              {{ t('settings.local.useMirror') }}
            </NCheckbox>
          </NSpace>
        </NFormItem>
      </NForm>
      <NText v-if="localModelsError" depth="3">{{ localModelsError }}</NText>
      <NDataTable
        v-else
        :columns="modelColumns"
        :data="localModels"
        :row-key="(row: LocalModelOut) => row.id"
        size="small"
      />
    </NCard>
  </NSpace>
</template>

<style scoped>
.settings {
  max-width: 960px;
  margin: 0 auto;
}
</style>
