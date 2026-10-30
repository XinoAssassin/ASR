<script setup lang="ts">
/**
 * Term sets page (PLAN §6.2): list, create, rename (the name field), delete; edit the terms in a
 * table (term, weight 1–5 or 50 boosted, note, aliases, blacklist) with the backend's validation;
 * paste / .txt import via `POST /api/term-sets/import` with per-line errors; .txt export in the
 * same line syntax. The edited set is a local draft until Save (the PUT replaces the whole list);
 * leaving or switching sets with unsaved changes asks first.
 */
import {
  NAlert,
  NButton,
  NCard,
  NDataTable,
  NDynamicTags,
  NEmpty,
  NForm,
  NFormItem,
  NInput,
  NModal,
  NPopconfirm,
  NSelect,
  NSpace,
  NSpin,
  NTag,
  NText,
  useDialog,
  useMessage,
  type DataTableColumns,
} from 'naive-ui'
import { computed, h, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router'

import { saveBlob, TermImportError, toApiError, type TermImportLineError, type TermSetOut } from '@/api'
import { useApiErrors } from '@/composables/useApiErrors'
import { useTermSetsStore } from '@/stores/termSets'
import {
  BOOSTED_WEIGHT,
  MAX_BOOSTED_TERMS,
  MAX_NAME_LENGTH,
  MAX_WORDS,
  WEIGHTS,
  boostedCount,
  duplicateKeys,
  exportFileName,
  isSavable,
  newRow,
  rowProblems,
  rowsFromTerms,
  serializeTerms,
  setProblems,
  termsFromRows,
  type RowProblem,
  type TermRow,
} from '@/utils/termSet'

const { t } = useI18n()
const message = useMessage()
const dialog = useDialog()
const { describe, notify } = useApiErrors()
const store = useTermSetsStore()
const route = useRoute()
const router = useRouter()

/** The line syntax (not translated; `|` cannot appear in vue-i18n messages). */
const SYNTAX = 'Target | alias | alias ~~~ black | black, weight, note'
const EXAMPLE = '通义千问 | 通益千问 | 同意千问 ~~~ 同意, 50, 产品名\nKubernetes | 酷伯尼特斯, 4\nPinia'
const PAGE_SIZE = 50

// --- selection & draft -------------------------------------------------------------------------

const selectedId = ref<string | null>(null)
const name = ref('')
const rows = ref<TermRow[]>([])
const baseline = ref('')
const saving = ref(false)
const filter = ref('')
const page = ref(1)

const selected = computed(() => store.items.find((s) => s.id === selectedId.value) ?? null)

function snapshot(): string {
  return JSON.stringify({ name: name.value.trim(), terms: termsFromRows(rows.value) })
}

const dirty = computed(() => selected.value !== null && snapshot() !== baseline.value)

function loadDraft(set: TermSetOut | null): void {
  name.value = set?.name ?? ''
  rows.value = rowsFromTerms(set?.terms ?? [])
  baseline.value = set ? snapshot() : ''
  filter.value = ''
  page.value = 1
}

function confirmDiscard(): Promise<boolean> {
  return new Promise((resolve) => {
    dialog.warning({
      title: t('termSets.unsaved'),
      content: t('termSets.unsavedLeave'),
      positiveText: t('termSets.leaveAnyway'),
      negativeText: t('termSets.stay'),
      onPositiveClick: () => resolve(true),
      onNegativeClick: () => resolve(false),
      onClose: () => resolve(false),
      onMaskClick: () => resolve(false),
    })
  })
}

async function select(id: string | null): Promise<void> {
  if (id === selectedId.value) return
  if (dirty.value && !(await confirmDiscard())) return
  selectedId.value = id
  loadDraft(store.items.find((s) => s.id === id) ?? null)
  void router.replace({ query: id ? { id } : {} })
}

function discard(): void {
  loadDraft(selected.value)
}

onBeforeRouteLeave(async () => (dirty.value ? confirmDiscard() : true))

// --- validation ----------------------------------------------------------------------------------

const dupKeys = computed(() => duplicateKeys(rows.value))
const problemsByKey = computed(() => {
  const map = new Map<number, RowProblem[]>()
  for (const r of rows.value) {
    const p = rowProblems(r, dupKeys.value.has(r.key))
    if (p.length) map.set(r.key, p)
  }
  return map
})
const nameProblems = computed(() => setProblems(name.value, rows.value))
const nameProblem = computed(() => {
  const p = nameProblems.value.find((x) => x !== 'tooManyTerms')
  return p ? t(`termSets.problems.${p}`) : null
})
const tooManyTerms = computed(() => nameProblems.value.includes('tooManyTerms'))
const savable = computed(() => isSavable(name.value, rows.value))
const boosted = computed(() => boostedCount(rows.value))
const termCount = computed(() => rows.value.filter((r) => r.term.trim()).length)

function isWarning(p: RowProblem): boolean {
  return p === 'duplicate'
}

// --- table ---------------------------------------------------------------------------------------

const visibleRows = computed(() => {
  const q = filter.value.trim().toLowerCase()
  if (!q) return rows.value
  return rows.value.filter((r) =>
    [r.term, r.note, ...r.aliases, ...r.blacklist].some((w) => w.toLowerCase().includes(q)),
  )
})

watch(filter, () => (page.value = 1))

const weightOptions = computed(() =>
  WEIGHTS.map((w) => ({
    value: w,
    label: w === BOOSTED_WEIGHT ? t('termSets.weightBoosted') : String(w),
  })),
)

function removeRow(key: number): void {
  rows.value = rows.value.filter((r) => r.key !== key)
}

function addRow(): void {
  filter.value = ''
  rows.value = [...rows.value, newRow()]
  page.value = Math.max(1, Math.ceil(rows.value.length / PAGE_SIZE))
}

function problemText(problems: RowProblem[] | undefined, only: RowProblem[]): string | null {
  const list = (problems ?? []).filter((p) => only.includes(p))
  return list.length ? list.map((p) => t(`termSets.problems.${p}`)).join(t('common.listSep')) : null
}

function hint(text: string | null, warning = false) {
  return text ? h(NText, { type: warning ? 'warning' : 'error', class: 'cell-hint' }, () => text) : null
}

const columns = computed<DataTableColumns<TermRow>>(() => [
  {
    title: t('termSets.col.term'),
    key: 'term',
    width: 200,
    render: (row) => {
      const p = problemsByKey.value.get(row.key)
      const err = problemText(p, ['termLength'])
      const warn = problemText(p, ['duplicate'])
      return h('div', [
        h(NInput, {
          value: row.term,
          size: 'small',
          status: err ? 'error' : warn ? 'warning' : undefined,
          'data-testid': 'term-input',
          onUpdateValue: (v: string) => (row.term = v),
        }),
        hint(err) ?? hint(warn, true),
      ])
    },
  },
  {
    title: t('termSets.col.weight'),
    key: 'weight',
    width: 120,
    render: (row) =>
      h(NSelect, {
        value: row.weight,
        size: 'small',
        options: weightOptions.value,
        'data-testid': 'term-weight',
        onUpdateValue: (v: number) => (row.weight = v),
      }),
  },
  {
    title: t('termSets.col.aliases'),
    key: 'aliases',
    render: (row) => {
      const err = problemText(problemsByKey.value.get(row.key), ['tooManyAliases', 'aliasLength'])
      return h('div', { title: t('termSets.aliasesHint') }, [
        h(NDynamicTags, {
          value: row.aliases,
          size: 'small',
          max: MAX_WORDS,
          'data-testid': 'term-aliases',
          onUpdateValue: (v: string[]) => (row.aliases = v),
        }),
        hint(err),
      ])
    },
  },
  {
    title: t('termSets.col.blacklist'),
    key: 'blacklist',
    render: (row) => {
      const err = problemText(problemsByKey.value.get(row.key), ['tooManyBlacklist', 'blacklistLength'])
      return h('div', { title: t('termSets.blacklistHint') }, [
        h(NDynamicTags, {
          value: row.blacklist,
          size: 'small',
          max: MAX_WORDS,
          type: 'warning',
          'data-testid': 'term-blacklist',
          onUpdateValue: (v: string[]) => (row.blacklist = v),
        }),
        hint(err),
      ])
    },
  },
  {
    title: t('termSets.col.note'),
    key: 'note',
    width: 180,
    render: (row) => {
      const err = problemText(problemsByKey.value.get(row.key), ['noteLength'])
      return h('div', [
        h(NInput, {
          value: row.note,
          size: 'small',
          status: err ? 'error' : undefined,
          'data-testid': 'term-note',
          onUpdateValue: (v: string) => (row.note = v),
        }),
        hint(err),
      ])
    },
  },
  {
    title: '',
    key: 'actions',
    width: 70,
    render: (row) =>
      h(
        NButton,
        {
          size: 'small',
          quaternary: true,
          type: 'error',
          'data-testid': 'term-remove',
          onClick: () => removeRow(row.key),
        },
        () => t('termSets.removeRow'),
      ),
  },
])

const pagination = computed(() => ({
  page: page.value,
  pageSize: PAGE_SIZE,
  onUpdatePage: (p: number) => (page.value = p),
}))

function rowClass(row: TermRow): string {
  const p = problemsByKey.value.get(row.key)
  if (!p) return ''
  return p.every(isWarning) ? 'row-warning' : 'row-error'
}

// --- save / delete / export ----------------------------------------------------------------------

async function save(): Promise<void> {
  const id = selectedId.value
  if (!id) return
  if (!savable.value) {
    message.error(t('termSets.fixProblems'))
    return
  }
  saving.value = true
  try {
    const saved = await store.save(id, { name: name.value.trim(), terms: termsFromRows(rows.value) })
    // The backend merges duplicates and cleans words: show what it stored.
    if (selectedId.value === id) loadDraft(saved)
    message.success(t('termSets.saved'))
  } catch (e) {
    if (toApiError(e).status === 404) {
      message.error(t('termSets.gone'))
      selectedId.value = null
      loadDraft(null)
      selectFirst()
    } else {
      notify(e, t('termSets.saveFailed'))
    }
  } finally {
    saving.value = false
  }
}

async function removeSet(): Promise<void> {
  const id = selectedId.value
  if (!id) return
  try {
    await store.remove(id)
    message.success(t('termSets.deleted'))
    selectedId.value = null
    loadDraft(null)
    selectFirst()
  } catch (e) {
    notify(e, t('termSets.saveFailed'))
  }
}

function exportTxt(): void {
  const { text, lossy } = serializeTerms(name.value.trim(), termsFromRows(rows.value))
  const file = exportFileName(name.value.trim())
  saveBlob(new Blob([text], { type: 'text/plain;charset=utf-8' }), file)
  if (lossy.length) {
    message.warning(t('termSets.exportLossy', { terms: lossy.join(t('common.listSep')) }), {
      duration: 8000,
      closable: true,
    })
  } else {
    message.success(t('termSets.exported', { name: file }))
  }
}

// --- create ------------------------------------------------------------------------------------

const createOpen = ref(false)
const createName = ref('')
const creating = ref(false)

function openCreate(): void {
  createName.value = ''
  createOpen.value = true
}

async function create(): Promise<void> {
  const n = createName.value.trim()
  if (!n) return
  creating.value = true
  try {
    const set = await store.create({ name: n, terms: [] })
    createOpen.value = false
    await select(set.id)
  } catch (e) {
    notify(e, t('termSets.saveFailed'))
  } finally {
    creating.value = false
  }
}

// --- import ------------------------------------------------------------------------------------

const importOpen = ref(false)
const importName = ref('')
const importText = ref('')
const importing = ref(false)
const importLines = ref<TermImportLineError[]>([])
const importError = ref<string | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)

function openImport(): void {
  importName.value = ''
  importText.value = ''
  importLines.value = []
  importError.value = null
  importOpen.value = true
}

async function onFile(e: Event): Promise<void> {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = '' // choosing the same file again fires `change` again
  if (!file) return
  importText.value = await file.text()
  if (!importName.value.trim()) importName.value = file.name.replace(/\.[^.]+$/, '')
}

watch([importText, importName], () => {
  importLines.value = []
  importError.value = null
})

async function runImport(): Promise<void> {
  const n = importName.value.trim()
  if (!n || !importText.value.trim()) return
  importing.value = true
  importLines.value = []
  importError.value = null
  try {
    const set = await store.importText(n, importText.value)
    importOpen.value = false
    message.success(t('termSets.imported', { name: set.name, n: set.terms.length }))
    await select(set.id)
  } catch (e) {
    if (e instanceof TermImportError) importLines.value = e.lines
    else importError.value = describe(e)
  } finally {
    importing.value = false
  }
}

// --- lifecycle ---------------------------------------------------------------------------------

function selectFirst(): void {
  const first = store.items[0]
  if (first) void select(first.id)
  else void router.replace({ query: {} })
}

onMounted(async () => {
  await store.load()
  const wanted = typeof route.query.id === 'string' ? route.query.id : null
  if (wanted && store.items.some((s) => s.id === wanted)) void select(wanted)
  else selectFirst()
})
</script>

<template>
  <div class="term-sets">
    <NAlert v-if="store.error" type="error" class="gap">{{ describe(store.error) }}</NAlert>
    <div class="layout">
      <!-- list -->
      <NCard size="small" :title="t('termSets.title')" class="list-card" data-testid="term-set-list">
        <template #header-extra>
          <NSpace size="small">
            <NButton size="small" type="primary" data-testid="term-set-create" @click="openCreate">
              {{ t('termSets.create') }}
            </NButton>
            <NButton size="small" data-testid="term-set-import" @click="openImport">
              {{ t('termSets.import') }}
            </NButton>
          </NSpace>
        </template>
        <NSpin :show="store.loading">
          <NEmpty v-if="!store.items.length" :description="t('termSets.empty')" />
          <ul v-else class="sets">
            <li
              v-for="set in store.items"
              :key="set.id"
              :class="{ active: set.id === selectedId }"
              data-testid="term-set-item"
              :data-id="set.id"
            >
              <button type="button" @click="select(set.id)">
                <span class="set-name">{{ set.name }}</span>
                <NText depth="3" class="set-count">{{ t('termSets.count', { n: set.terms.length }) }}</NText>
              </button>
            </li>
          </ul>
        </NSpin>
        <NText depth="3" class="intro">{{ t('termSets.intro') }}</NText>
      </NCard>

      <!-- editor -->
      <NCard v-if="selected" size="small" class="editor-card" data-testid="term-set-editor">
        <template #header>
          <NSpace align="center" size="small">
            <span>{{ selected.name }}</span>
            <NTag v-if="dirty" size="small" type="warning" :bordered="false" data-testid="term-set-dirty">
              {{ t('termSets.unsaved') }}
            </NTag>
          </NSpace>
        </template>
        <template #header-extra>
          <NSpace size="small">
            <NButton size="small" data-testid="term-set-export" @click="exportTxt">
              {{ t('termSets.export') }}
            </NButton>
            <NPopconfirm @positive-click="removeSet">
              <template #trigger>
                <NButton size="small" quaternary type="error" data-testid="term-set-delete">
                  {{ t('termSets.delete') }}
                </NButton>
              </template>
              {{ t('termSets.deleteConfirm', { name: selected.name }) }}
            </NPopconfirm>
          </NSpace>
        </template>

        <NForm label-placement="left" label-width="auto" class="name-form">
          <NFormItem
            :label="t('termSets.name')"
            :validation-status="nameProblem ? 'error' : undefined"
            :feedback="nameProblem ?? undefined"
          >
            <NInput
              v-model:value="name"
              :maxlength="MAX_NAME_LENGTH + 20"
              :placeholder="t('termSets.namePlaceholder')"
              data-testid="term-set-name"
              style="max-width: 360px"
            />
          </NFormItem>
        </NForm>

        <NAlert type="info" :title="t('termSets.weightTitle')" :show-icon="false" class="gap">
          {{ t('termSets.weightHelp') }}
        </NAlert>

        <NSpace align="center" justify="space-between" class="gap">
          <NSpace align="center" size="small">
            <NInput
              v-model:value="filter"
              size="small"
              clearable
              :placeholder="t('termSets.filter')"
              style="width: 200px"
              data-testid="term-filter"
            />
            <NText depth="3" data-testid="term-count">{{ t('termSets.count', { n: termCount }) }}</NText>
            <NTag
              size="small"
              :type="boosted > MAX_BOOSTED_TERMS ? 'warning' : 'default'"
              :bordered="false"
              :title="boosted > MAX_BOOSTED_TERMS ? t('termSets.boostedOver') : undefined"
              data-testid="boosted-count"
            >
              {{ t('termSets.boostedCount', { n: boosted }) }}
            </NTag>
          </NSpace>
          <NButton size="small" data-testid="term-add" @click="addRow">{{ t('termSets.addRow') }}</NButton>
        </NSpace>

        <NAlert v-if="tooManyTerms" type="error" :show-icon="false" class="gap">
          {{ t('termSets.problems.tooManyTerms') }}
        </NAlert>
        <NAlert v-if="boosted > MAX_BOOSTED_TERMS" type="warning" :show-icon="false" class="gap">
          {{ t('termSets.boostedOver') }}
        </NAlert>

        <NDataTable
          :columns="columns"
          :data="visibleRows"
          :row-key="(row: TermRow) => row.key"
          :row-class-name="rowClass"
          :pagination="pagination"
          size="small"
          data-testid="term-table"
        />

        <NSpace justify="end" class="actions">
          <NButton :disabled="!dirty" data-testid="term-set-discard" @click="discard">
            {{ t('termSets.discard') }}
          </NButton>
          <NButton
            type="primary"
            :loading="saving"
            :disabled="!dirty"
            data-testid="term-set-save"
            @click="save"
          >
            {{ t('termSets.save') }}
          </NButton>
        </NSpace>
      </NCard>
      <NCard v-else size="small" class="editor-card">
        <NEmpty :description="t('termSets.empty')" />
      </NCard>
    </div>

    <!-- create -->
    <NModal
      v-model:show="createOpen"
      preset="card"
      :title="t('termSets.createTitle')"
      style="width: min(420px, calc(100vw - 32px))"
      data-testid="term-set-create-dialog"
    >
      <NInput
        v-model:value="createName"
        :maxlength="MAX_NAME_LENGTH"
        :placeholder="t('termSets.namePlaceholder')"
        data-testid="term-set-create-name"
        @keydown.enter="create"
      />
      <template #footer>
        <NSpace justify="end">
          <NButton @click="createOpen = false">{{ t('common.cancel') }}</NButton>
          <NButton
            type="primary"
            :loading="creating"
            :disabled="!createName.trim()"
            data-testid="term-set-create-submit"
            @click="create"
          >
            {{ t('termSets.create') }}
          </NButton>
        </NSpace>
      </template>
    </NModal>

    <!-- import -->
    <NModal
      v-model:show="importOpen"
      preset="card"
      :title="t('termSets.importTitle')"
      style="width: min(680px, calc(100vw - 32px))"
      data-testid="term-set-import-dialog"
    >
      <NSpace vertical>
        <NText depth="3">{{ t('termSets.importHint') }}</NText>
        <pre class="syntax">{{ SYNTAX }}</pre>
        <NText depth="3">{{ t('termSets.importExampleNote') }}</NText>
        <pre class="syntax">{{ EXAMPLE }}</pre>
        <NInput
          v-model:value="importName"
          :maxlength="MAX_NAME_LENGTH"
          :placeholder="t('termSets.importName')"
          data-testid="term-import-name"
        />
        <NSpace align="center">
          <NButton size="small" data-testid="term-import-file-button" @click="fileInput?.click()">
            {{ t('termSets.importFile') }}
          </NButton>
          <input
            ref="fileInput"
            type="file"
            accept=".txt,text/plain"
            class="file-input"
            data-testid="term-import-file"
            @change="onFile"
          />
        </NSpace>
        <NInput
          v-model:value="importText"
          type="textarea"
          :autosize="{ minRows: 6, maxRows: 14 }"
          :placeholder="t('termSets.importText')"
          data-testid="term-import-text"
        />
        <NAlert v-if="importError" type="error" :show-icon="false">{{ importError }}</NAlert>
        <NAlert
          v-if="importLines.length"
          type="error"
          :title="t('termSets.importLineErrors')"
          data-testid="term-import-errors"
        >
          <table class="line-errors">
            <tr v-for="err in importLines" :key="err.line" data-testid="term-import-error">
              <td class="line-no">{{ t('termSets.line', { n: err.line }) }}</td>
              <td><code>{{ err.input }}</code></td>
              <td>{{ err.message }}</td>
            </tr>
          </table>
        </NAlert>
      </NSpace>
      <template #footer>
        <NSpace justify="end">
          <NButton @click="importOpen = false">{{ t('common.cancel') }}</NButton>
          <NButton
            type="primary"
            :loading="importing"
            :disabled="!importName.trim() || !importText.trim()"
            data-testid="term-import-submit"
            @click="runImport"
          >
            {{ t('termSets.importSubmit') }}
          </NButton>
        </NSpace>
      </template>
    </NModal>
  </div>
</template>

<style scoped>
.term-sets {
  max-width: 1400px;
  margin: 0 auto;
}

.layout {
  display: grid;
  grid-template-columns: 280px minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}

@media (max-width: 900px) {
  .layout {
    grid-template-columns: minmax(0, 1fr);
  }
}

.gap {
  margin-bottom: 12px;
}

.sets {
  list-style: none;
  margin: 0 0 12px;
  padding: 0;
}

.sets li button {
  display: flex;
  width: 100%;
  justify-content: space-between;
  align-items: baseline;
  gap: 8px;
  padding: 8px 10px;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.sets li button:hover {
  background: rgba(128, 128, 128, 0.1);
}

.sets li.active button {
  background: rgba(24, 160, 88, 0.14);
  font-weight: 600;
}

.set-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.set-count {
  flex: none;
  font-size: 12px;
}

.intro {
  display: block;
  font-size: 12px;
}

.name-form {
  margin-bottom: -12px;
}

.actions {
  margin-top: 12px;
}

.syntax {
  margin: 0;
  padding: 8px 10px;
  border-radius: 4px;
  background: rgba(128, 128, 128, 0.1);
  font-size: 12px;
  white-space: pre-wrap;
}

.file-input {
  display: none;
}

.line-errors {
  border-collapse: collapse;
}

.line-errors td {
  padding: 2px 8px 2px 0;
  vertical-align: top;
}

.line-no {
  white-space: nowrap;
}

:deep(.cell-hint) {
  display: block;
  font-size: 12px;
  margin-top: 2px;
}

:deep(.row-error td) {
  background: rgba(208, 48, 80, 0.06);
}

:deep(.row-warning td) {
  background: rgba(240, 160, 32, 0.06);
}
</style>
