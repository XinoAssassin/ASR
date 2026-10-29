<script setup lang="ts">
import {
  NAlert,
  NButton,
  NCard,
  NCollapse,
  NCollapseItem,
  NEmpty,
  NInput,
  NProgress,
  NSpace,
  NTag,
  NText,
  useMessage,
} from 'naive-ui'
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'

import DirectoryPicker from '@/components/DirectoryPicker.vue'
import JobOptionsForm from '@/components/JobOptionsForm.vue'
import ProbeCard from '@/components/ProbeCard.vue'
import SourceTips from '@/components/SourceTips.vue'
import { createJob } from '@/api'
import { useApiErrors } from '@/composables/useApiErrors'
import { useSettingsStore } from '@/stores/settings'
import { useTermSetsStore } from '@/stores/termSets'
import { useUploadsStore, type QueueItem } from '@/stores/uploads'
import { ACCEPT_ATTRIBUTE, ACCEPTED_EXTENSIONS } from '@/utils/audio'
import { formatBytes } from '@/utils/format'
import { buildJobCreate } from '@/utils/jobForm'
import { parseTerms } from '@/utils/terms'

const { t } = useI18n()
const message = useMessage()
const router = useRouter()
const { describe, notify } = useApiErrors()
const uploads = useUploadsStore()
const settings = useSettingsStore()
const termSets = useTermSetsStore()

// --- file selection ---------------------------------------------------------------------------

const fileInput = ref<HTMLInputElement | null>(null)
const dragging = ref(false)

function addFiles(list: FileList | null | undefined): void {
  if (!list?.length) return
  const rejected = uploads.addFiles(Array.from(list))
  if (rejected.length) {
    message.warning(t('upload.files.rejected', { names: rejected.join('、') }), { duration: 6000 })
  }
}

function onInputChange(ev: Event): void {
  const input = ev.target as HTMLInputElement
  addFiles(input.files)
  input.value = ''
}

function onDrop(ev: DragEvent): void {
  dragging.value = false
  addFiles(ev.dataTransfer?.files)
}

function statusType(item: QueueItem): 'default' | 'info' | 'success' | 'error' | 'warning' {
  switch (item.status) {
    case 'uploading':
      return 'info'
    case 'done':
      return 'success'
    case 'error':
      return 'error'
    case 'cancelled':
      return 'warning'
    default:
      return 'default'
  }
}

function percent(item: QueueItem): number {
  return item.total ? Math.floor((item.loaded / item.total) * 100) : item.status === 'done' ? 100 : 0
}

const canUpload = computed(
  () => !!uploads.targetDir.trim() && uploads.pendingCount > 0 && !uploads.uploading,
)

async function startUpload(): Promise<void> {
  await uploads.uploadAll()
  const failed = uploads.queue.filter((i) => i.status === 'error')
  if (failed.length) message.error(t('upload.files.someFailed', { n: failed.length }))
}

// --- register a file already on the server ----------------------------------------------------

const registerPath = ref('')

async function register(): Promise<void> {
  if (!registerPath.value.trim()) return
  try {
    await uploads.register(registerPath.value)
    registerPath.value = ''
    message.success(t('upload.register.done'))
  } catch (e) {
    notify(e, t('upload.register.failed'))
  }
}

// --- job creation -----------------------------------------------------------------------------

const termsParsed = computed(() => parseTerms(uploads.jobForm.extraTerms))
const submitting = ref(false)
const canStart = computed(
  () =>
    uploads.ready.length > 0 &&
    !submitting.value &&
    !uploads.uploading &&
    termsParsed.value.errors.length === 0,
)

async function startJobs(): Promise<void> {
  submitting.value = true
  const submitted: string[] = []
  try {
    for (const entry of uploads.ready) {
      const body = buildJobCreate(
        uploads.jobForm,
        { uploadId: entry.upload.id, channels: entry.channels, streamIndex: entry.streamIndex },
        termsParsed.value.terms,
      )
      try {
        await createJob(body)
        submitted.push(entry.upload.id)
      } catch (e) {
        notify(e, t('upload.start.failed', { name: entry.upload.original_name }))
      }
    }
  } finally {
    submitting.value = false
  }
  if (submitted.length) {
    uploads.clearSubmitted(submitted)
    message.success(t('upload.start.done', { n: submitted.length }))
    if (!uploads.ready.length) void router.push({ name: 'jobs' })
  }
}

// --- init -------------------------------------------------------------------------------------

function initProvider(): void {
  if (uploads.providerInitialized || !settings.server) return
  uploads.jobForm.provider = settings.defaultProvider
  uploads.providerInitialized = true
}

watch(() => settings.server, initProvider)

onMounted(() => {
  if (!settings.server) void settings.load()
  else initProvider()
  void termSets.load()
})
</script>

<template>
  <NSpace vertical size="large" class="upload">
    <NCard :title="t('upload.dir.title')">
      <DirectoryPicker v-model="uploads.targetDir" :recent="uploads.recentDirs" />
    </NCard>

    <NCard :title="t('upload.files.title')">
      <div
        class="drop-zone"
        :class="{ dragging }"
        data-testid="drop-zone"
        @dragover.prevent="dragging = true"
        @dragleave.prevent="dragging = false"
        @drop.prevent="onDrop"
        @click="fileInput?.click()"
      >
        <input
          ref="fileInput"
          type="file"
          multiple
          :accept="ACCEPT_ATTRIBUTE"
          class="file-input"
          data-testid="file-input"
          @change="onInputChange"
          @click.stop
        />
        <div class="drop-title">{{ t('upload.files.drop') }}</div>
        <NText depth="3">
          {{ t('upload.files.accepted', { list: ACCEPTED_EXTENSIONS.join(' / ') }) }}
        </NText>
      </div>

      <div v-if="uploads.queue.length" class="queue">
        <div
          v-for="item in uploads.queue"
          :key="item.key"
          class="queue-item"
          data-testid="upload-item"
          :data-status="item.status"
        >
          <NSpace justify="space-between" align="center" :wrap="false">
            <NSpace align="center" size="small" :wrap="false" class="queue-name">
              <NTag size="small" :type="statusType(item)">
                {{ t(`upload.status.${item.status}`) }}
              </NTag>
              <span class="name">{{ item.file.name }}</span>
              <NText depth="3">{{ formatBytes(item.file.size) }}</NText>
              <NText v-if="item.status === 'uploading' && item.totalParts > 1" depth="3">
                {{ t('upload.files.part', { n: item.part + 1, total: item.totalParts }) }}
              </NText>
            </NSpace>
            <NSpace size="small" :wrap="false">
              <NButton
                v-if="item.status === 'uploading'"
                size="tiny"
                quaternary
                @click="uploads.cancelItem(item.key)"
              >
                {{ t('common.cancel') }}
              </NButton>
              <NButton
                v-else
                size="tiny"
                quaternary
                :disabled="uploads.uploading"
                @click="uploads.removeItem(item.key)"
              >
                {{ t('common.remove') }}
              </NButton>
            </NSpace>
          </NSpace>
          <NProgress
            type="line"
            :percentage="percent(item)"
            :status="item.status === 'error' ? 'error' : item.status === 'done' ? 'success' : 'default'"
            :height="6"
            :show-indicator="item.status === 'uploading'"
            data-testid="upload-progress"
          />
          <NText v-if="item.error" type="error" class="item-error">{{ describe(item.error) }}</NText>
        </div>
      </div>

      <NSpace align="center" class="actions">
        <NButton
          type="primary"
          :disabled="!canUpload"
          :loading="uploads.uploading"
          data-testid="upload-button"
          @click="startUpload"
        >
          {{ t('upload.files.upload') }}
        </NButton>
        <NText v-if="!uploads.targetDir.trim()" depth="3">{{ t('upload.files.needDir') }}</NText>
      </NSpace>

      <NCollapse class="register">
        <NCollapseItem :title="t('upload.register.title')" name="register">
          <NSpace vertical>
            <NText depth="3">{{ t('upload.register.hint') }}</NText>
            <NSpace :wrap="false">
              <NInput
                v-model:value="registerPath"
                :placeholder="t('upload.register.placeholder')"
                data-testid="register-path-input"
                style="min-width: 420px"
                @keyup.enter="register"
              />
              <NButton
                :loading="uploads.registering"
                :disabled="!registerPath.trim()"
                data-testid="register-button"
                @click="register"
              >
                {{ t('upload.register.submit') }}
              </NButton>
            </NSpace>
          </NSpace>
        </NCollapseItem>
      </NCollapse>
    </NCard>

    <NCard :title="t('upload.ready.title')">
      <NEmpty v-if="!uploads.ready.length" :description="t('upload.ready.empty')" />
      <NSpace v-else vertical>
        <ProbeCard
          v-for="entry in uploads.ready"
          :key="entry.upload.id"
          v-model:channels="entry.channels"
          v-model:stream-index="entry.streamIndex"
          :upload="entry.upload"
          :source="entry.source"
          :provider="uploads.jobForm.provider"
          :max-file-size-mb="settings.cloudMaxFileSizeMb"
          @remove="uploads.removeReady(entry.upload.id)"
        />
      </NSpace>
      <SourceTips class="tips" />
    </NCard>

    <NCard :title="t('jobOptions.title')">
      <NAlert type="warning" :show-icon="false" class="privacy">
        {{ t('settings.cloud.privacy') }}
      </NAlert>
      <JobOptionsForm
        v-model="uploads.jobForm"
        :term-sets="termSets.items"
        :term-sets-error="termSets.error ? describe(termSets.error) : null"
      />
      <NSpace align="center">
        <NButton
          type="primary"
          size="large"
          :disabled="!canStart"
          :loading="submitting"
          data-testid="start-job-button"
          @click="startJobs"
        >
          {{ t('upload.start.button', { n: uploads.ready.length }) }}
        </NButton>
        <NText v-if="!uploads.ready.length" depth="3">{{ t('upload.start.needFile') }}</NText>
      </NSpace>
    </NCard>
  </NSpace>
</template>

<style scoped>
.upload {
  max-width: 1080px;
  margin: 0 auto;
}

.drop-zone {
  position: relative;
  border: 2px dashed rgba(128, 128, 128, 0.4);
  border-radius: 6px;
  padding: 28px 16px;
  text-align: center;
  cursor: pointer;
  transition: border-color 0.2s, background-color 0.2s;
}

.drop-zone:hover,
.drop-zone.dragging {
  border-color: #18a058;
  background-color: rgba(24, 160, 88, 0.06);
}

.drop-title {
  font-size: 16px;
  margin-bottom: 6px;
}

/* Kept in the DOM (not display:none) so automation can set files on it. */
.file-input {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
  pointer-events: none;
}

.queue {
  margin-top: 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.queue-name {
  min-width: 0;
}

.name {
  word-break: break-all;
}

.item-error {
  font-size: 12px;
}

.actions {
  margin-top: 16px;
}

.register {
  margin-top: 16px;
}

.tips {
  margin-top: 16px;
}

.privacy {
  margin-bottom: 16px;
}
</style>
