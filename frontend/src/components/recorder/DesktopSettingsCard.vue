<script setup lang="ts">
/**
 * Settings → "桌面录音" (desktop only): what the Rust core uses for its own background uploads
 * (`backend_set_config`, docs/desktop-ipc.md). The connection (URL + X-API-Key) is not edited
 * here — it comes from the browser settings above, which stay the single source of truth.
 */
import {
  NAlert,
  NButton,
  NCard,
  NDescriptions,
  NDescriptionsItem,
  NForm,
  NFormItem,
  NSelect,
  NSpace,
  NSwitch,
  NTag,
  NText,
  useMessage,
} from 'naive-ui'
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import DirectoryPicker from '@/components/DirectoryPicker.vue'
import JobOptionsForm from '@/components/JobOptionsForm.vue'
import type { ChannelsOption } from '@/api'
import { useApiErrors } from '@/composables/useApiErrors'
import { desktopBackendUrl, useRecorderStore } from '@/stores/recorder'
import { useSettingsStore } from '@/stores/settings'
import { useTermSetsStore } from '@/stores/termSets'
import { useUploadsStore } from '@/stores/uploads'
import { parseTerms } from '@/utils/terms'

import { fromJobConfig, toJobConfig, type RecordingJobSettings } from './recordingJob'

const { t } = useI18n()
const message = useMessage()
const recorder = useRecorderStore()
const settings = useSettingsStore()
const uploads = useUploadsStore()
const termSets = useTermSetsStore()
const { describe } = useApiErrors()

const targetDir = ref('')
const autoUpload = ref(true)
const job = ref<RecordingJobSettings>(fromJobConfig({}))

const channelOptions = computed<{ label: string; value: ChannelsOption }[]>(() => [
  { label: t('channelsOption.per_channel'), value: 'per_channel' },
  { label: t('recorder.settings.channelsAuto'), value: 'auto' },
  { label: t('channelsOption.downmix'), value: 'downmix' },
])

// The URL Rust gets on the next sync; `settings.backendUrl` makes it react to the browser setting.
const pendingUrl = computed(() => {
  void settings.backendUrl
  return desktopBackendUrl()
})

function fill(): void {
  const c = recorder.config
  if (!c) return
  targetDir.value = c.targetDir
  autoUpload.value = c.autoUpload
  job.value = fromJobConfig(c, settings.defaultProvider)
  if (!c.jobOptions && c.provider == null) job.value.createJob = true // first run: suggest a job
}

watch(() => recorder.config, fill, { immediate: true })

const termErrors = computed(() =>
  job.value.createJob ? parseTerms(job.value.form.extraTerms).errors.length : 0,
)

async function save(): Promise<void> {
  try {
    await recorder.saveConfig({
      targetDir: targetDir.value.trim(),
      autoUpload: autoUpload.value,
      ...toJobConfig(job.value),
    })
    message.success(t('settings.saved'))
  } catch (e) {
    message.error(t('common.labelDetail', { label: t('recorder.settings.syncFailed'), detail: (e as Error).message }))
  }
}

async function sync(): Promise<void> {
  try {
    await recorder.syncBackend()
    message.success(t('recorder.settings.synced'))
  } catch (e) {
    message.error(t('common.labelDetail', { label: t('recorder.settings.syncFailed'), detail: (e as Error).message }))
  }
}

onMounted(() => {
  void recorder.init()
  void termSets.load()
})
</script>

<template>
  <NCard :title="t('recorder.settings.title')" data-testid="desktop-settings">
    <NText depth="3" class="intro">{{ t('recorder.settings.intro') }}</NText>
    <NAlert v-if="!recorder.config && recorder.error" type="error" class="block">
      {{ t('common.labelDetail', { label: t('recorder.settings.loadFailed'), detail: recorder.error }) }}
    </NAlert>

    <NDescriptions :column="1" bordered size="small" label-placement="left" class="block">
      <NDescriptionsItem :label="t('recorder.settings.backendUrl')">
        <NSpace align="center" size="small">
          <span data-testid="desktop-backend-url">{{ recorder.config?.baseUrl ?? '—' }}</span>
          <NTag v-if="recorder.config && recorder.config.baseUrl !== pendingUrl" size="small" type="warning">
            → {{ pendingUrl }}
          </NTag>
        </NSpace>
      </NDescriptionsItem>
      <NDescriptionsItem :label="t('recorder.settings.apiKey')">
        <NTag size="small" :type="recorder.keySet ? 'success' : 'default'" data-testid="desktop-key-state">
          {{ recorder.keySet ? t('recorder.settings.keySet') : t('recorder.settings.keyNotSet') }}
        </NTag>
      </NDescriptionsItem>
    </NDescriptions>
    <NSpace align="center" class="block">
      <NButton size="small" :loading="recorder.savingConfig" data-testid="desktop-sync" @click="sync">
        {{ t('recorder.settings.sync') }}
      </NButton>
      <NText depth="3" class="hint">{{ t('recorder.settings.connectionHint') }}</NText>
    </NSpace>

    <NForm label-placement="left" label-width="120" :disabled="!recorder.config">
      <NFormItem :label="t('recorder.settings.targetDir')">
        <DirectoryPicker v-model="targetDir" :recent="uploads.recentDirs" style="width: 100%" />
      </NFormItem>
      <NFormItem :label="t('recorder.settings.autoUpload')">
        <NSpace align="center">
          <NSwitch v-model:value="autoUpload" data-testid="desktop-auto-upload" />
          <NText depth="3">{{ t('recorder.settings.autoUploadHint') }}</NText>
        </NSpace>
      </NFormItem>
      <NFormItem :label="t('recorder.settings.createJob')">
        <NSpace align="center">
          <NSwitch v-model:value="job.createJob" data-testid="desktop-create-job" />
          <NText depth="3">{{ t('recorder.settings.createJobHint') }}</NText>
        </NSpace>
      </NFormItem>
      <NFormItem v-if="job.createJob" :label="t('recorder.settings.channels')">
        <NSpace vertical style="width: 100%">
          <NSelect
            v-model:value="job.channels"
            :options="channelOptions"
            data-testid="desktop-channels"
            style="max-width: 320px"
          />
          <NText depth="3" class="hint">{{ t('recorder.settings.channelsMonoNote') }}</NText>
        </NSpace>
      </NFormItem>
    </NForm>

    <template v-if="job.createJob">
      <JobOptionsForm
        v-model="job.form"
        :term-sets="termSets.items"
        :term-sets-error="termSets.error ? describe(termSets.error) : null"
      />
    </template>

    <NSpace align="center">
      <NButton
        type="primary"
        :disabled="!recorder.config || termErrors > 0"
        :loading="recorder.savingConfig"
        data-testid="desktop-settings-save"
        @click="save"
      >
        {{ t('recorder.settings.save') }}
      </NButton>
    </NSpace>
  </NCard>
</template>

<style scoped>
.intro {
  display: block;
  margin-bottom: 12px;
}

.block {
  margin-bottom: 16px;
}

.hint {
  font-size: 12px;
}
</style>
