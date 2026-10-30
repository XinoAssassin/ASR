<script setup lang="ts">
/**
 * "Send to SiYuan" (D61): notebook (closed ones disabled, default = the configured one), optional
 * document path (the placeholder previews the path template), heading locale and the export
 * options. Pending edits are flushed first (`flush`), so SiYuan gets what the user sees. The
 * backend pushes the document itself: a 422 means "not configured" (link to Settings), a 502
 * that the backend host cannot reach SiYuan.
 *
 * The source file name for the path preview comes from `getUpload` (a read-only lookup owned by
 * this dialog, see `api/endpoints.ts`).
 */
import {
  NAlert,
  NButton,
  NForm,
  NFormItem,
  NInput,
  NModal,
  NRadio,
  NRadioGroup,
  NSelect,
  NSpace,
  NSwitch,
  NText,
} from 'naive-ui'
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { RouterLink } from 'vue-router'

import { getUpload, toApiError, type ApiError, type ExportLocale, type SiYuanPushOut } from '@/api'
import { useApiErrors } from '@/composables/useApiErrors'
import { useSiyuanStore } from '@/stores/siyuan'
import { pathValues, renderPathTemplate } from '@/utils/siyuanPath'

const props = defineProps<{
  show: boolean
  transcriptId: string
  provider: string
  uploadId: string | null
  fileUrl: string | null
  createdAt: string
  /** Initial values: the export card's options and the UI locale. */
  timestamps: boolean
  mergeSameSpeaker: boolean
  locale: ExportLocale
  /** Save pending edits; rejects when they could not be saved. */
  flush: () => Promise<void>
}>()

const emit = defineEmits<{ 'update:show': [value: boolean] }>()

const { t } = useI18n()
const { describe } = useApiErrors()
const siyuan = useSiyuanStore()

const notebook = ref<string | null>(null)
const path = ref('')
const locale = ref<ExportLocale>('zh')
const timestamps = ref(true)
const merge = ref(false)
const result = ref<SiYuanPushOut | null>(null)
const failure = ref<ApiError | null>(null)
const flushFailed = ref(false)
const sourceName = ref<string | null>(null)
const sourceCreatedAt = ref<string | null>(null)

const NOT_CONFIGURED = /not configured/i

const notConfigured = computed(
  () =>
    (siyuan.settings !== null && !siyuan.settings.token_set) ||
    [failure.value, siyuan.notebooksError].some(
      (e) => e?.status === 422 && NOT_CONFIGURED.test(e.detail ?? ''),
    ),
)

const notebookOptions = computed(() =>
  siyuan.notebooks.map((nb) => ({
    value: nb.id,
    disabled: nb.closed,
    label: nb.closed
      ? t('siyuan.notebookClosed', { name: nb.name })
      : nb.id === siyuan.settings?.notebook
        ? t('siyuan.notebookDefault', { name: nb.name })
        : nb.name,
  })),
)

const preview = computed(() => {
  const template = siyuan.settings?.path_template
  if (!template) return null
  return renderPathTemplate(
    template,
    pathValues({
      sourceName: sourceName.value,
      createdAt: sourceCreatedAt.value ?? props.createdAt,
      provider: props.provider,
    }),
  )
})

const pathError = computed(() => {
  const p = path.value.trim()
  return p && !p.startsWith('/') ? t('siyuan.pathInvalid') : null
})

const canSubmit = computed(
  () => !notConfigured.value && !pathError.value && !siyuan.pushing && !!notebook.value,
)

function fileNameOfUrl(url: string): string | null {
  try {
    const name = decodeURIComponent(new URL(url).pathname.split('/').pop() ?? '')
    return name || null
  } catch {
    return null
  }
}

async function loadSource(): Promise<void> {
  sourceName.value = props.fileUrl ? fileNameOfUrl(props.fileUrl) : null
  sourceCreatedAt.value = null
  if (!props.uploadId) return
  try {
    const upload = await getUpload(props.uploadId)
    sourceName.value = upload.original_name
    sourceCreatedAt.value = upload.created_at
  } catch {
    // Only the preview needs it; the backend renders the real path.
  }
}

async function open(): Promise<void> {
  result.value = null
  failure.value = null
  flushFailed.value = false
  path.value = ''
  locale.value = props.locale
  timestamps.value = props.timestamps
  merge.value = props.mergeSameSpeaker
  void loadSource()
  await siyuan.load()
  notebook.value = siyuan.settings?.notebook ?? null
  if (siyuan.settings?.token_set) await siyuan.loadNotebooks()
  // The configured notebook may be gone or closed: then let the user choose. (When the list
  // could not be read, keep the default: the push reports the real problem.)
  const usable = siyuan.notebooks.filter((nb) => !nb.closed)
  if (!siyuan.notebooksError && !usable.some((nb) => nb.id === notebook.value)) {
    notebook.value = usable.length === 1 ? usable[0]!.id : null
  }
}

watch(
  () => props.show,
  (show) => {
    if (show) void open()
  },
  { immediate: true },
)

async function submit(): Promise<void> {
  if (!canSubmit.value || !notebook.value) return
  failure.value = null
  flushFailed.value = false
  result.value = null
  try {
    await props.flush()
  } catch {
    flushFailed.value = true
    return
  }
  try {
    result.value = await siyuan.push(props.transcriptId, {
      notebook: notebook.value,
      path: path.value.trim() || null,
      locale: locale.value,
      timestamps: timestamps.value,
      merge_same_speaker: merge.value,
    })
  } catch (e) {
    failure.value = toApiError(e)
  }
}

function close(): void {
  emit('update:show', false)
}
</script>

<template>
  <NModal
    :show="show"
    preset="card"
    :title="t('siyuan.dialogTitle')"
    style="width: min(560px, calc(100vw - 32px))"
    :mask-closable="!siyuan.pushing"
    data-testid="siyuan-dialog"
    @update:show="(v: boolean) => emit('update:show', v)"
  >
    <NAlert v-if="notConfigured" type="warning" data-testid="siyuan-not-configured" class="gap">
      {{ t('siyuan.notConfigured') }}
      <RouterLink :to="{ name: 'settings', hash: '#siyuan' }" @click="close">
        {{ t('siyuan.goToSettings') }}
      </RouterLink>
    </NAlert>

    <template v-if="result">
      <NAlert type="success" :title="t('siyuan.sent')" data-testid="siyuan-result" class="gap">
        <div>{{ t('siyuan.sentPath', { path: result.path }) }}</div>
        <a :href="result.url" data-testid="siyuan-open" :title="t('siyuan.openHint')">{{ t('siyuan.open') }}</a>
      </NAlert>
      <NAlert
        v-if="result.warnings?.length"
        type="warning"
        :title="t('siyuan.warnings')"
        data-testid="siyuan-warnings"
        class="gap"
      >
        <div v-for="(w, i) in result.warnings" :key="i">{{ w }}</div>
      </NAlert>
    </template>

    <NForm v-else label-placement="left" label-width="auto" :disabled="notConfigured || siyuan.pushing">
      <NFormItem :label="t('siyuan.notebook')">
        <NSpace vertical style="width: 100%">
          <NSelect
            v-model:value="notebook"
            :options="notebookOptions"
            :loading="siyuan.notebooksLoading"
            :placeholder="t('siyuan.notebookPlaceholder')"
            data-testid="siyuan-notebook"
          />
          <NText
            v-if="siyuan.notebooksError && !notConfigured"
            type="error"
            data-testid="siyuan-notebooks-error"
          >
            {{ t('common.labelDetail', { label: t('siyuan.notebooksFailed'), detail: describe(siyuan.notebooksError) }) }}
          </NText>
        </NSpace>
      </NFormItem>
      <NFormItem
        :label="t('siyuan.path')"
        :validation-status="pathError ? 'error' : undefined"
        :feedback="pathError ?? undefined"
      >
        <NSpace vertical style="width: 100%">
          <NInput
            v-model:value="path"
            clearable
            :placeholder="preview ?? siyuan.settings?.path_template ?? ''"
            data-testid="siyuan-path"
          />
          <NText depth="3" class="small">
            <template v-if="preview">{{ t('siyuan.pathPreview', { path: preview }) }}<br /></template>
            {{ t('siyuan.pathHint') }}
          </NText>
        </NSpace>
      </NFormItem>
      <NFormItem :label="t('siyuan.locale')">
        <NRadioGroup v-model:value="locale" data-testid="siyuan-locale">
          <NRadio value="zh">{{ t('siyuan.localeZh') }}</NRadio>
          <NRadio value="en">{{ t('siyuan.localeEn') }}</NRadio>
        </NRadioGroup>
      </NFormItem>
      <NFormItem :label="t('transcript.includeTimestamps')">
        <NSwitch v-model:value="timestamps" data-testid="siyuan-timestamps" />
      </NFormItem>
      <NFormItem :label="t('transcript.mergeSameSpeaker')">
        <NSwitch v-model:value="merge" data-testid="siyuan-merge" />
      </NFormItem>
    </NForm>

    <NAlert v-if="flushFailed" type="error" :show-icon="false" data-testid="siyuan-flush-failed" class="gap">
      {{ t('siyuan.flushFailed') }}
    </NAlert>
    <NAlert
      v-if="failure && !notConfigured"
      type="error"
      :title="t('siyuan.failed')"
      data-testid="siyuan-error"
      class="gap"
    >
      <div v-if="failure.status === 502">{{ t('siyuan.unreachable') }}</div>
      <div>{{ describe(failure) }}</div>
    </NAlert>

    <template #footer>
      <NSpace justify="end">
        <NButton :disabled="siyuan.pushing" @click="close">
          {{ result ? t('common.close') : t('common.cancel') }}
        </NButton>
        <NButton
          v-if="!result"
          type="primary"
          :loading="siyuan.pushing"
          :disabled="!canSubmit"
          data-testid="siyuan-submit"
          @click="submit"
        >
          {{ t('siyuan.submit') }}
        </NButton>
      </NSpace>
    </template>
  </NModal>
</template>

<style scoped>
.gap {
  margin-bottom: 12px;
}

.small {
  font-size: 12px;
}
</style>
