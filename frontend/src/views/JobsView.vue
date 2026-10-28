<script setup lang="ts">
import {
  NAlert,
  NButton,
  NCard,
  NCheckbox,
  NDataTable,
  NProgress,
  NSelect,
  NSpace,
  NTag,
  NText,
  NTooltip,
  type DataTableColumns,
  useDialog,
  useMessage,
} from 'naive-ui'
import { computed, h, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { RouterLink } from 'vue-router'

import { isTerminal, type JobOut, type JobStatus } from '@/api'
import { useApiErrors } from '@/composables/useApiErrors'
import { useNow } from '@/composables/useNow'
import { useJobsStore } from '@/stores/jobs'
import { basename, elapsedSeconds, formatDateTime, formatDuration } from '@/utils/format'

const { t, te, locale } = useI18n()
const message = useMessage()
const dialog = useDialog()
const { describe, notify } = useApiErrors()
const jobs = useJobsStore()
const now = useNow()

const STATUSES: JobStatus[] = ['queued', 'running', 'succeeded', 'failed', 'cancelled']

const statusOptions = computed(() => [
  { label: t('jobs.allStatuses'), value: '' },
  ...STATUSES.map((s) => ({ label: t(`jobStatus.${s}`), value: s })),
])

const STATUS_TYPE: Record<JobStatus, 'default' | 'info' | 'success' | 'error' | 'warning'> = {
  queued: 'default',
  running: 'info',
  succeeded: 'success',
  failed: 'error',
  cancelled: 'warning',
}

function stageLabel(stage: string): string {
  return te(`stage.${stage}`) ? t(`stage.${stage}`) : stage
}

function fileName(job: JobOut): string {
  if (job.upload_id) {
    const upload = jobs.uploads[job.upload_id]
    return upload?.original_name ?? job.upload_id
  }
  return basename(job.file_url) || job.file_url || '—'
}

function duration(job: JobOut): string {
  const upload = job.upload_id ? jobs.uploads[job.upload_id] : null
  return formatDuration(upload?.probe?.duration)
}

async function run(action: () => Promise<void>, context: string): Promise<void> {
  try {
    await action()
  } catch (e) {
    notify(e, context)
  }
}

function confirmDelete(job: JobOut): void {
  const deleteAudio = ref(false)
  dialog.warning({
    title: t('jobs.deleteTitle'),
    content: () =>
      h(NSpace, { vertical: true }, () => [
        h('div', t('jobs.deleteConfirm', { name: fileName(job) })),
        job.upload_id
          ? h(
              NCheckbox,
              {
                checked: deleteAudio.value,
                'onUpdate:checked': (v: boolean) => (deleteAudio.value = v),
                'data-testid': 'delete-audio-checkbox',
              },
              () => t('jobs.deleteAudio'),
            )
          : null,
      ]),
    positiveText: t('common.delete'),
    negativeText: t('common.cancel'),
    positiveButtonProps: { type: 'error', 'data-testid': 'delete-confirm' } as never,
    onPositiveClick: async () => {
      try {
        await jobs.remove(job.id, deleteAudio.value)
        message.success(t('jobs.deleted'))
      } catch (e) {
        notify(e, t('jobs.deleteFailed'))
      }
    },
  })
}

const columns = computed<DataTableColumns<JobOut>>(() => [
  {
    title: t('jobs.col.file'),
    key: 'file',
    minWidth: 200,
    ellipsis: { tooltip: true },
    render: (job) => h('span', { 'data-testid': 'job-file' }, fileName(job)),
  },
  {
    title: t('jobs.col.provider'),
    key: 'provider',
    width: 90,
    render: (job) => t(`provider.${job.provider}`),
  },
  {
    title: t('jobs.col.status'),
    key: 'status',
    width: 160,
    render: (job) =>
      h(NSpace, { vertical: true, size: 2 }, () => [
        h(
          NTag,
          {
            type: STATUS_TYPE[job.status],
            size: 'small',
            'data-testid': 'job-status',
            'data-status': job.status,
          },
          () => t(`jobStatus.${job.status}`),
        ),
        job.error
          ? h(
              NTooltip,
              { style: { maxWidth: '480px' } },
              {
                trigger: () =>
                  h(NText, { type: 'error', class: 'job-error', 'data-testid': 'job-error' }, () =>
                    job.error,
                  ),
                default: () => job.error,
              },
            )
          : null,
      ]),
  },
  {
    title: t('jobs.col.stage'),
    key: 'stage',
    minWidth: 200,
    render: (job) =>
      h(NSpace, { vertical: true, size: 2 }, () => [
        h(NText, { 'data-testid': 'job-stage' }, () => stageLabel(job.stage)),
        h(NProgress, {
          type: 'line',
          percentage: Math.round(Math.min(1, Math.max(0, job.progress)) * 100),
          status:
            job.status === 'failed' ? 'error' : job.status === 'succeeded' ? 'success' : 'default',
          height: 6,
          processing: job.status === 'running',
        }),
        job.message && !isTerminal(job.status)
          ? h(NText, { depth: 3, class: 'job-message' }, () => job.message)
          : null,
      ]),
  },
  {
    title: t('jobs.col.duration'),
    key: 'duration',
    width: 90,
    render: (job) => duration(job),
  },
  {
    title: t('jobs.col.elapsed'),
    key: 'elapsed',
    width: 90,
    render: (job) => formatDuration(elapsedSeconds(job.started_at, job.finished_at, now.value)),
  },
  {
    title: t('jobs.col.created'),
    key: 'created_at',
    width: 170,
    render: (job) => formatDateTime(job.created_at, locale.value),
  },
  {
    title: t('jobs.col.actions'),
    key: 'actions',
    width: 220,
    render: (job) =>
      h(NSpace, { size: 4, wrap: false }, () => [
        job.status === 'succeeded' && job.transcript_id
          ? h(
              RouterLink,
              { to: { name: 'transcript', params: { id: job.transcript_id } } },
              () =>
                h(
                  NButton,
                  { size: 'small', type: 'primary', 'data-testid': 'job-open-transcript' },
                  () => t('jobs.open'),
                ),
            )
          : null,
        !isTerminal(job.status)
          ? h(
              NButton,
              {
                size: 'small',
                'data-testid': 'job-cancel',
                onClick: () => run(() => jobs.cancel(job.id), t('jobs.cancelFailed')),
              },
              () => t('common.cancel'),
            )
          : null,
        job.status === 'failed' || job.status === 'cancelled'
          ? h(
              NButton,
              {
                size: 'small',
                'data-testid': 'job-retry',
                onClick: () => run(() => jobs.retry(job.id), t('jobs.retryFailed')),
              },
              () => t('jobs.retry'),
            )
          : null,
        h(
          NButton,
          {
            size: 'small',
            quaternary: true,
            type: 'error',
            'data-testid': 'job-delete',
            onClick: () => confirmDelete(job),
          },
          () => t('common.delete'),
        ),
      ]),
  },
])

const pagination = computed(() => ({
  page: jobs.page,
  pageSize: jobs.pageSize,
  itemCount: jobs.total,
  showSizePicker: true,
  pageSizes: [10, 20, 50, 100],
  onUpdatePage: (p: number) => void jobs.setPage(p),
  onUpdatePageSize: (s: number) => void jobs.setPageSize(s),
}))

function rowProps(job: JobOut): Record<string, string> {
  return { 'data-testid': 'job-row', 'data-job-id': job.id, 'data-status': job.status }
}

onMounted(() => {
  jobs.startLive()
  void jobs.load()
})
onBeforeUnmount(() => jobs.stopLive())
</script>

<template>
  <NCard :title="t('views.jobs.title')" class="jobs">
    <template #header-extra>
      <NSpace align="center">
        <NSelect
          :value="jobs.status ?? ''"
          :options="statusOptions"
          size="small"
          style="width: 140px"
          data-testid="job-status-filter"
          @update:value="(v: string) => jobs.setStatus((v || null) as JobStatus | null)"
        />
        <NButton size="small" :loading="jobs.loading" @click="jobs.load()">
          {{ t('common.refresh') }}
        </NButton>
      </NSpace>
    </template>
    <NAlert v-if="jobs.error" type="error" style="margin-bottom: 12px">
      {{ describe(jobs.error) }}
    </NAlert>
    <NDataTable
      remote
      :columns="columns"
      :data="jobs.items"
      :loading="jobs.loading && !jobs.items.length"
      :pagination="pagination"
      :row-key="(job: JobOut) => job.id"
      :row-props="rowProps"
      :scroll-x="1200"
      data-testid="jobs-table"
    />
  </NCard>
</template>

<style scoped>
.jobs {
  max-width: 1400px;
  margin: 0 auto;
}

:deep(.job-error) {
  font-size: 12px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

:deep(.job-message) {
  font-size: 12px;
}
</style>
