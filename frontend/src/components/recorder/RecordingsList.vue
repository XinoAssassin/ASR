<script setup lang="ts">
/**
 * Recordings in the output folder with their upload state (kept current by
 * `recordings://changed`). Rust owns the upload queue; this list only queues / retries, opens the
 * folder and links to the job or transcript.
 */
import { NButton, NEmpty, NSpace, NTag, NText, NTooltip, useMessage } from 'naive-ui'
import { watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { RouterLink } from 'vue-router'

import type { Recording, UploadState } from '@/platform'
import { useRecorderStore } from '@/stores/recorder'
import { formatBytes, formatDateTime, formatDuration } from '@/utils/format'

const { t, locale } = useI18n()
const message = useMessage()
const recorder = useRecorderStore()

const STATE_TYPE: Record<UploadState, 'default' | 'info' | 'success' | 'error' | 'warning'> = {
  pending: 'default',
  uploading: 'info',
  uploaded: 'info',
  transcribing: 'info',
  done: 'success',
  failed: 'error',
}

function canUpload(rec: Recording): boolean {
  return rec.upload.state === 'pending' || rec.upload.state === 'failed'
}

async function upload(rec: Recording): Promise<void> {
  try {
    await recorder.upload(rec.id)
    message.success(t('recorder.list.queued'))
  } catch (e) {
    message.error(t('common.labelDetail', { label: t('recorder.list.failed'), detail: (e as Error).message }))
  }
}

async function reveal(rec: Recording): Promise<void> {
  try {
    await recorder.reveal(rec.id)
  } catch (e) {
    message.error(t('common.labelDetail', { label: t('recorder.list.failed'), detail: (e as Error).message }))
  }
}

/** `upload.transcriptId` from Rust; for older cores, looked up from the job. */
function transcriptOf(rec: Recording): string | null {
  const { transcriptId, jobId } = rec.upload
  return transcriptId ?? (jobId ? (recorder.transcriptIds[jobId] ?? null) : null)
}

function time(iso: string | null): string {
  return formatDateTime(iso, locale.value)
}

// Finished jobs link straight to their transcript; fall back to the job when Rust has no id.
watch(
  () =>
    recorder.recordings.filter(
      (r) => r.upload.state === 'done' && r.upload.jobId && !r.upload.transcriptId,
    ),
  (done) => {
    for (const rec of done) {
      const jobId = rec.upload.jobId!
      if (!recorder.transcriptIds[jobId]) void recorder.lookupTranscript(jobId)
    }
  },
  { immediate: true },
)
</script>

<template>
  <NEmpty v-if="!recorder.recordings.length" :description="t('recorder.list.empty')" />
  <div v-else class="list" data-testid="recordings-list">
    <div
      v-for="rec in recorder.recordings"
      :key="rec.id"
      class="row"
      data-testid="recording-item"
      :data-state="rec.upload.state"
    >
      <div class="main">
        <div class="name" :title="rec.path">{{ rec.name }}</div>
        <NText depth="3" class="meta">
          {{ time(rec.createdAt) }} · {{ formatDuration(rec.duration) }} · {{ formatBytes(rec.size) }}
        </NText>
      </div>
      <div class="state">
        <NSpace size="small" align="center" :wrap="false">
          <NTag size="small" :type="STATE_TYPE[rec.upload.state]" data-testid="recording-upload-state">
            {{ t(`recorder.uploadState.${rec.upload.state}`) }}
          </NTag>
          <NText v-if="rec.upload.attempts > 1" depth="3" class="meta">
            {{ t('recorder.list.attempts', { n: rec.upload.attempts }) }}
          </NText>
        </NSpace>
        <NText v-if="rec.upload.error" type="error" class="meta error" data-testid="recording-error">
          {{ rec.upload.error }}
        </NText>
        <NText v-if="rec.upload.nextRetryAt && rec.upload.state !== 'done'" depth="3" class="meta">
          {{ t('recorder.list.nextRetry', { time: time(rec.upload.nextRetryAt) }) }}
        </NText>
      </div>
      <NSpace size="small" :wrap="false" class="actions">
        <NButton
          v-if="canUpload(rec)"
          size="small"
          :type="rec.upload.state === 'failed' ? 'warning' : 'primary'"
          secondary
          data-testid="recording-upload"
          @click="upload(rec)"
        >
          {{ rec.upload.state === 'failed' ? t('recorder.list.retry') : t('recorder.list.uploadNow') }}
        </NButton>
        <RouterLink
          v-if="transcriptOf(rec)"
          :to="{ name: 'transcript', params: { id: transcriptOf(rec)! } }"
          data-testid="recording-transcript"
        >
          <NButton size="small" type="primary">{{ t('recorder.list.openTranscript') }}</NButton>
        </RouterLink>
        <RouterLink v-else-if="rec.upload.jobId" :to="{ name: 'jobs' }" data-testid="recording-job">
          <NButton size="small">{{ t('recorder.list.openJob') }}</NButton>
        </RouterLink>
        <NTooltip>
          <template #trigger>
            <NButton size="small" quaternary data-testid="recording-reveal" @click="reveal(rec)">
              {{ t('recorder.list.reveal') }}
            </NButton>
          </template>
          {{ rec.path }}
        </NTooltip>
      </NSpace>
    </div>
  </div>
</template>

<style scoped>
.list {
  display: flex;
  flex-direction: column;
}

.row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(160px, auto) auto;
  gap: 12px;
  align-items: center;
  padding: 10px 0;
  border-bottom: 1px solid rgba(128, 128, 128, 0.18);
}

.row:last-child {
  border-bottom: none;
}

.name {
  font-weight: 500;
  word-break: break-all;
}

.meta {
  font-size: 12px;
}

.state {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.error {
  word-break: break-word;
}

@media (max-width: 720px) {
  .row {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
