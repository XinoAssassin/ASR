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
  NSelect,
  NSpace,
  NTag,
  NText,
  type DataTableColumns,
  useMessage,
} from 'naive-ui'
import { computed, h, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute } from 'vue-router'

import { type DeviceId, type LocalModelOut } from '@/api'
import BackendProfilesCard from '@/components/BackendProfilesCard.vue'
import EnvironmentCard from '@/components/EnvironmentCard.vue'
import EnvTag from '@/components/EnvTag.vue'
import OnlineServicesCard from '@/components/OnlineServicesCard.vue'
import DesktopSettingsCard from '@/components/recorder/DesktopSettingsCard.vue'
import SidecarCard from '@/components/recorder/SidecarCard.vue'
import SiyuanSettingsCard from '@/components/SiyuanSettingsCard.vue'
import { useApiErrors } from '@/composables/useApiErrors'
import { useDeviceOptions } from '@/composables/useDeviceOptions'
import { isDesktop } from '@/platform'
import { useHardwareStore } from '@/stores/hardware'
import { useHealthStore } from '@/stores/health'
import { useSettingsStore } from '@/stores/settings'
import { envVariable } from '@/utils/envFacts'
import { deviceLabel } from '@/utils/hardware'

import { createFormSync } from './formSync'

const { t } = useI18n()
const message = useMessage()
const { describe, notify } = useApiErrors()
const health = useHealthStore()
const settings = useSettingsStore()
const hardware = useHardwareStore()
const desktop = isDesktop()
const route = useRoute()

/** Environment variable behind a settings field (`env_only`), for the "env" markers. */
function envOf(field: string): string | null {
  return envVariable(settings.server?.env_only, field)
}

// --- local models (status only; management in Settings arrives with M6) ----------------------

const useMirror = ref(true)
const localForm = reactive({ asrModel: '', device: 'cpu' as DeviceId })
let pollTimer: ReturnType<typeof setInterval> | null = null

/** cpu / cuda / rocm, unavailable ones disabled with the backend's reason. */
const localDeviceOptions = useDeviceOptions(() => settings.server?.local.device)

/** "ROCm · AMD Radeon 8060S" for the effective settings device. */
const currentDeviceText = computed(() => {
  const id = settings.server?.local.device
  if (!id) return '—'
  return deviceLabel(id, hardware.info?.devices.find((d) => d.id === id)?.name)
})

const localModelsError = computed(() =>
  settings.localModelsError ? describe(settings.localModelsError) : null,
)
const asrModelOptions = computed(() => [
  { label: t('settings.local.auto'), value: '' },
  ...settings.localModels
    .filter((m) => m.kind === 'asr')
    .map((m) => {
      const notes = [
        isWhisper(m.id) && t('settings.local.englishOnly'),
        !m.downloaded && t('settings.local.notDownloaded'),
      ].filter(Boolean)
      const label = notes.length
        ? t('common.withNote', { text: m.id, note: notes.join(t('common.listSep')) })
        : m.id
      return { label, value: m.id }
    }),
])

/** faster-whisper engines (M11): English only, need the backend's `whisper` extra. */
function isWhisper(id: string): boolean {
  return id.startsWith('faster-whisper')
}

function fillLocalForm(): boolean {
  const s = settings.server
  if (!s) return false
  localForm.asrModel = s.local.asr_model ?? ''
  localForm.device = s.local.device
  return true
}

const localSync = createFormSync(localForm, fillLocalForm)

watch(
  () => settings.server,
  () => localSync.sync(),
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
      isWhisper(row.id)
        ? t('common.withNote', { text: row.name, note: t('settings.local.whisperNote') })
        : row.name,
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
          disabled: row.downloaded || settings.anyDownloading,
          loading: row.status === 'downloading',
          onClick: () => void startDownload(row.id),
          'data-testid': `download-${row.id}`,
        },
        () => t('settings.local.download'),
      ),
  },
])

function schedulePoll(): void {
  if (settings.anyDownloading && pollTimer === null) {
    pollTimer = setInterval(() => void refreshLocalModels(), 3000)
  } else if (!settings.anyDownloading && pollTimer !== null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function startDownload(key: string): Promise<void> {
  try {
    await settings.downloadLocalModel(key, useMirror.value)
    schedulePoll()
  } catch (e) {
    notify(e, t('settings.local.downloadFailed'))
  }
}

async function saveLocal(): Promise<void> {
  try {
    await settings.save({ local: { asr_model: localForm.asrModel, device: localForm.device } })
    localSync.refill()
    message.success(t('settings.saved'))
  } catch (e) {
    notify(e, t('settings.saveFailed'))
  }
}

onBeforeUnmount(() => {
  if (pollTimer !== null) clearInterval(pollTimer)
})

async function refreshLocalModels(): Promise<void> {
  await settings.loadLocalModels()
  schedulePoll()
}

onMounted(() => {
  void settings.load()
  void hardware.load()
  void refreshLocalModels()
  // Deep links: #siyuan (SiYuan dialog), #backends / #sidecar (top-bar backend menu),
  // #local-models (managed backend wizard: "下载模型").
  const anchor = route.hash.slice(1)
  if (anchor) {
    void nextTick(() => document.getElementById(anchor)?.scrollIntoView({ block: 'start' }))
  }
})
</script>

<template>
  <NSpace vertical size="large" class="settings">
    <!-- Backend profiles + the active one's URL / key (stored in this browser) -->
    <BackendProfilesCard @key-changed="refreshLocalModels">
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
          <EnvTag v-if="envOf('api_key_required')" :name="envOf('api_key_required')!" />
        </NDescriptionsItem>
      </NDescriptions>
      <NAlert v-else-if="health.state === 'error'" type="error">
        {{ t('health.error') }} — {{ health.error }}
      </NAlert>
    </BackendProfilesCard>

    <!-- Desktop app only: what the Rust core uses for background uploads -->
    <DesktopSettingsCard v-if="desktop" />
    <SidecarCard v-if="desktop" />

    <!-- Server-side: default provider, cloud recognition, LLM -->
    <OnlineServicesCard />

    <!-- Local models: download state; downloads go through `asr models download` (rule 6) -->
    <NCard id="local-models" :title="t('settings.local.title')">
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
          <span data-testid="local-device-current">{{ currentDeviceText }}</span>
        </NDescriptionsItem>
        <NDescriptionsItem :label="t('settings.local.cacheDir')">
          {{ settings.server.local.model_cache_dir }}
          <EnvTag v-if="envOf('local.model_cache_dir')" :name="envOf('local.model_cache_dir')!" />
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
          <NSpace vertical style="width: 100%">
            <NSelect
              v-model:value="localForm.device"
              :options="localDeviceOptions"
              data-testid="local-device"
              style="max-width: 420px"
            />
            <NText v-if="hardware.loaded && !hardware.supported" depth="3" data-testid="hardware-unknown">
              {{ t('hardware.unknown') }}
            </NText>
          </NSpace>
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
        :data="settings.localModels"
        :row-key="(row: LocalModelOut) => row.id"
        size="small"
      />
    </NCard>

    <!-- SiYuan export (the backend pushes the documents itself) -->
    <SiyuanSettingsCard />

    <!-- Read-only: only environment variables set these -->
    <EnvironmentCard
      v-if="settings.server?.environment"
      :environment="settings.server.environment"
      :env-only="settings.server.env_only ?? {}"
    />
  </NSpace>
</template>

<style scoped>
.settings {
  max-width: 960px;
  margin: 0 auto;
}

.small {
  font-size: 12px;
}
</style>
