<script setup lang="ts">
/**
 * Transcript page (M3 player + M7 proofreading): waveform with speaker regions, dialogue with
 * inline editing (autosaved), split / merge, speaker reassign / rename / merge / add, search and
 * replace, term highlighting, correction review with "add as alias", opt-in LLM suggestions,
 * revert and export. Shortcuts: Space play/pause, ←/→ previous/next segment, Enter edit the
 * current segment, Esc stop editing, Ctrl+Enter split at the caret, Ctrl+F search.
 */
import {
  NAlert,
  NButton,
  NButtonGroup,
  NCard,
  NEmpty,
  NInput,
  NPopconfirm,
  NSpace,
  NSpin,
  NSwitch,
  NTag,
  NText,
  useDialog,
  useMessage,
} from 'naive-ui'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { RouterLink, onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'

import {
  toApiError,
  type Correction,
  type ExportFormat,
  type Segment,
  type Speaker,
  type TranscriptOp,
} from '@/api'
import CorrectionsPanel from '@/components/CorrectionsPanel.vue'
import {
  clampMatchIndex,
  isImeKey,
  isInteractiveTarget,
  isPopupOpen,
  matchCounter,
  nextMatchIndex,
  shortcutAction,
} from '@/components/interaction'
import SegmentRow from '@/components/SegmentRow.vue'
import SpeakerPanel from '@/components/SpeakerPanel.vue'
import SummaryPanel from '@/components/SummaryPanel.vue'
import WaveformPlayer, { type WaveRegion } from '@/components/WaveformPlayer.vue'
import { useApiErrors } from '@/composables/useApiErrors'
import { useTermSetsStore } from '@/stores/termSets'
import { useTranscriptStore } from '@/stores/transcript'
import { formatDateTime, formatDuration } from '@/utils/format'
import { correctionMarks, pieces, searchMarks, termMarks, type Mark, type Piece } from '@/utils/highlight'
import { findMatches } from '@/utils/search'
import { findActiveSegment } from '@/utils/segments'

const props = defineProps<{ id: string }>()

const { t, locale } = useI18n()
const message = useMessage()
const dialog = useDialog()
const { describe, notify } = useApiErrors()
const store = useTranscriptStore()
const termSets = useTermSetsStore()

const player = ref<InstanceType<typeof WaveformPlayer> | null>(null)
const listEl = ref<HTMLElement | null>(null)
const searchInput = ref<InstanceType<typeof NInput> | null>(null)
const currentTime = ref(0)
const playing = ref(false)
const playerError = ref<string | null>(null)
const editingId = ref<string | null>(null)
const focusedCorrection = ref<string | null>(null)
const focusSeq = ref(0)
/** A split / merge is in flight; further ones are ignored until it settles (no double merge). */
const structuralPending = ref(false)

const segments = computed<Segment[]>(() =>
  [...(store.transcript?.segments ?? [])].sort((a, b) => a.start - b.start),
)

const regions = computed<WaveRegion[]>(() =>
  segments.value.map((s) => ({
    id: s.id,
    start: s.start,
    end: s.end,
    color: store.speakerMap.get(s.speaker_id)?.color ?? '#888888',
  })),
)

const activeIndex = computed(() => findActiveSegment(segments.value, currentTime.value))
const activeId = computed(() => segments.value[activeIndex.value]?.id ?? null)

const speakerList = computed(() => {
  const stats = new Map<string, { count: number; seconds: number }>()
  for (const s of segments.value) {
    const entry = stats.get(s.speaker_id) ?? { count: 0, seconds: 0 }
    entry.count++
    entry.seconds += Math.max(0, s.end - s.start)
    stats.set(s.speaker_id, entry)
  }
  return [...store.speakerMap.values()].map((sp) => ({
    ...sp,
    count: stats.get(sp.id)?.count ?? 0,
    seconds: stats.get(sp.id)?.seconds ?? 0,
  }))
})

const hasAudioSource = computed(() => !!store.job?.upload_id)
const corrections = computed<Correction[]>(() => store.transcript?.corrections ?? [])
const segmentTexts = computed(() => Object.fromEntries(segments.value.map((s) => [s.id, s.text])))

function speakerOf(id: string): Speaker {
  return store.speakerMap.get(id) ?? { id, name: id, color: '#888888' }
}

// --- search / replace ---------------------------------------------------------------------------

const query = ref('')
const replacement = ref('')
const matchIndex = ref(0)
/** The current match has been scrolled to (so the next Enter / ↓ moves on). */
const matchRevealed = ref(false)
const matches = computed(() => findMatches(segments.value, query.value))
const currentMatch = computed(
  () => matches.value[clampMatchIndex(matchIndex.value, matches.value.length)] ?? null,
)
const matchCountText = computed(() => matchCounter(matchIndex.value, matches.value.length))

watch(query, () => {
  matchIndex.value = 0
  matchRevealed.value = false
})
// Edits / replace can shrink the match list under the current index.
watch(
  () => matches.value.length,
  (n) => (matchIndex.value = clampMatchIndex(matchIndex.value, n)),
)

function stepMatch(delta: number): void {
  if (!matches.value.length) return
  matchIndex.value = nextMatchIndex(matchIndex.value, delta, matches.value.length, matchRevealed.value)
  matchRevealed.value = true
  const m = currentMatch.value
  if (m) void scrollTo(m.segmentId)
}

function onSearchKeydown(e: KeyboardEvent): void {
  if (e.key !== 'Enter' || isImeKey(e)) return
  e.preventDefault()
  stepMatch(e.shiftKey ? -1 : 1)
}

async function replaceAll(): Promise<void> {
  const find = query.value.trim()
  if (!find) return
  const n = matches.value.length
  try {
    // Case-insensitive, like the search that counted and highlighted the matches.
    await store.applyOps([{ op: 'replace', find, replace: replacement.value, ignore_case: true }])
    message.success(t('proofread.replaced', { n }))
  } catch (e) {
    notifySaveError(e)
  }
}

// --- highlights ---------------------------------------------------------------------------------

const correctionsBySegment = computed(() => {
  const map = new Map<string, Correction[]>()
  for (const c of corrections.value) map.set(c.segment_id, [...(map.get(c.segment_id) ?? []), c])
  return map
})

function piecesFor(seg: Segment): Piece[] {
  const marks: Mark[] = [
    ...termMarks(seg.text, store.terms),
    ...correctionMarks(seg.text, correctionsBySegment.value.get(seg.id) ?? []),
  ]
  const q = query.value.trim()
  if (q) {
    marks.push(...searchMarks(seg.text, q))
    const m = currentMatch.value
    if (m && m.segmentId === seg.id) marks.push({ start: m.start, end: m.end, kind: 'current' })
  }
  return pieces(seg.text, marks)
}

// --- navigation ---------------------------------------------------------------------------------

async function scrollTo(segmentId: string): Promise<void> {
  await nextTick()
  const el = listEl.value?.querySelector<HTMLElement>(`[data-segment-id="${CSS.escape(segmentId)}"]`)
  el?.scrollIntoView({ block: 'center', behavior: 'smooth' })
}

function seekTo(seg: Segment): void {
  player.value?.seek(seg.start, true)
  currentTime.value = seg.start
}

function stepSegment(delta: number): void {
  if (!segments.value.length) return
  const base = activeIndex.value >= 0 ? activeIndex.value : findPreviousIndex()
  const next = Math.min(segments.value.length - 1, Math.max(0, base + delta))
  const seg = segments.value[next]
  if (seg) {
    seekTo(seg)
    void scrollTo(seg.id)
  }
}

/** Index of the last segment that starts before the playhead (for gaps between segments). */
function findPreviousIndex(): number {
  let index = -1
  for (let i = 0; i < segments.value.length; i++) {
    if (segments.value[i]!.start <= currentTime.value) index = i
    else break
  }
  return index
}

watch(activeId, async (id) => {
  if (!id || !playing.value || editingId.value) return
  await scrollTo(id)
})

// --- editing ------------------------------------------------------------------------------------

/** 409: the store has already reloaded the server's version (unsent text kept on top). */
function isConflict(e: unknown): boolean {
  return toApiError(e).status === 409
}

function notifySaveError(e: unknown): void {
  if (!isConflict(e)) return notify(e, t('proofread.saveFailed'))
  message.warning(t('proofread.conflictReloaded'), { duration: 8000, closable: true })
}

async function run(op: () => Promise<void>): Promise<void> {
  try {
    await op()
  } catch (e) {
    notifySaveError(e)
  }
}

const saveConflict = computed(() => store.saveState === 'error' && store.saveError?.status === 409)
const retrying = ref(false)

async function retrySave(): Promise<void> {
  retrying.value = true
  try {
    await store.retrySave()
  } catch (e) {
    notifySaveError(e)
  } finally {
    retrying.value = false
  }
}

function startEdit(seg: Segment): void {
  if (editingId.value && editingId.value !== seg.id) void store.flush()
  editingId.value = seg.id
}

function stopEdit(): void {
  editingId.value = null
  void run(() => store.flush())
}

/** Run a split / merge unless one is still pending (a double-click must not merge twice). */
function structural(op: TranscriptOp): void {
  if (structuralPending.value) return
  structuralPending.value = true
  editingId.value = null
  void run(() => store.applyOps([op])).finally(() => (structuralPending.value = false))
}

/** `at` is a code-point offset (SegmentRow converts the textarea's UTF-16 caret). */
function split(seg: Segment, at: number): void {
  structural({ op: 'split', segment_id: seg.id, at })
}

function mergeNext(seg: Segment): void {
  structural({ op: 'merge', segment_id: seg.id })
}

function setSpeaker(seg: Segment, speakerId: string): void {
  void run(() => store.applyOps([{ op: 'set_speaker', segment_id: seg.id, speaker_id: speakerId }]))
}

function newSpeakerName(): string {
  return t('proofread.speakerN', { n: (store.transcript?.speakers.length ?? 0) + 1 })
}

function newSpeakerFor(seg: Segment): void {
  const id = store.nextSpeakerId()
  void run(async () => {
    await store.saveSpeakers([{ id, name: newSpeakerName() }])
    await store.applyOps([{ op: 'set_speaker', segment_id: seg.id, speaker_id: id }])
  })
}

function addSpeaker(): void {
  void run(() => store.saveSpeakers([{ id: store.nextSpeakerId(), name: newSpeakerName() }]))
}

function renameSpeaker(id: string, name: string): void {
  void run(() => store.saveSpeakers([{ id, name }]))
}

function mergeSpeakers(fromId: string, intoId: string): void {
  void run(() => store.applyOps([{ op: 'merge_speakers', from_id: fromId, into_id: intoId }]))
}

function revertAll(): void {
  editingId.value = null
  void run(async () => {
    await store.revert()
    message.success(t('proofread.reverted'))
  })
}

// --- corrections --------------------------------------------------------------------------------

function review(id: string, decision: 'accept' | 'reject'): void {
  void run(() => store.applyOps([{ op: 'review_correction', correction_id: id, decision }]))
}

function jumpToSegment(segmentId: string): void {
  const seg = segments.value.find((s) => s.id === segmentId)
  if (seg) seekTo(seg)
  void scrollTo(segmentId)
}

function focusCorrection(id: string): void {
  focusedCorrection.value = id
  focusSeq.value++
}

async function addAlias(c: Correction, termSetId: string | null): Promise<void> {
  try {
    const set = await termSets.addAlias(termSetId, c.after, c.before, t('proofread.defaultSetName'))
    message.success(t('proofread.aliasAdded', { alias: c.before, term: c.after, set: set.name }))
  } catch (e) {
    notify(e, t('proofread.aliasFailed'))
  }
}

// --- summary (M11) -----------------------------------------------------------------------------

const summaryStale = computed(() => {
  const tr = store.transcript
  return !!tr?.summary && Date.parse(tr.updated_at) > Date.parse(tr.summary.created_at)
})

async function summarize(): Promise<void> {
  try {
    await store.summarize()
    message.success(t('summary.done'))
  } catch (e) {
    notify(e, t('summary.failed'))
  }
}

async function removeSummary(): Promise<void> {
  try {
    await store.removeSummary()
    message.success(t('summary.removed'))
  } catch (e) {
    notify(e, t('summary.failed'))
  }
}

async function suggest(): Promise<void> {
  try {
    const r = await store.suggestWithLlm(null)
    message.success(t('proofread.llmDone', { added: r.added, discarded: r.discarded }))
  } catch (e) {
    notify(e, t('proofread.llmFailed'))
  }
}

// --- keyboard -----------------------------------------------------------------------------------

function onKeydown(e: KeyboardEvent): void {
  const action = shortcutAction(e, {
    interactiveTarget: isInteractiveTarget(e.target),
    popupOpen: isPopupOpen(),
  })
  switch (action) {
    case 'search':
      e.preventDefault()
      searchInput.value?.focus()
      break
    case 'playPause':
      e.preventDefault()
      player.value?.playPause()
      break
    case 'prev':
      e.preventDefault()
      stepSegment(-1)
      break
    case 'next':
      e.preventDefault()
      stepSegment(1)
      break
    case 'edit': {
      const seg = segments.value[activeIndex.value >= 0 ? activeIndex.value : 0]
      if (seg) {
        e.preventDefault()
        startEdit(seg)
      }
      break
    }
  }
}

// --- export -------------------------------------------------------------------------------------

const EXPORT_FORMATS: ExportFormat[] = ['txt', 'srt', 'vtt', 'json']
const exportTimestamps = ref(true)
const exportMerge = ref(false)

async function doExport(format: ExportFormat): Promise<void> {
  try {
    const name = await store.exportAs({
      format,
      timestamps: exportTimestamps.value,
      mergeSameSpeaker: exportMerge.value,
    })
    message.success(t('transcript.exported', { name }))
  } catch (e) {
    notify(e, t('transcript.exportFailed'))
  }
}

const SAVE_TYPE = { idle: 'default', saving: 'info', saved: 'success', error: 'error' } as const

watch(
  () => props.id,
  (id) => {
    currentTime.value = 0
    playerError.value = null
    editingId.value = null
    query.value = ''
    void store.load(id)
  },
)

function confirmDiscard(): Promise<boolean> {
  return new Promise((resolve) => {
    dialog.warning({
      title: t('proofread.unsavedTitle'),
      content: t('proofread.unsavedLeave'),
      positiveText: t('proofread.leaveAnyway'),
      negativeText: t('proofread.stay'),
      onPositiveClick: () => resolve(true),
      onNegativeClick: () => resolve(false),
      onClose: () => resolve(false),
      onMaskClick: () => resolve(false),
    })
  })
}

/**
 * Save what was typed (including the last < AUTOSAVE_MS of keystrokes) before the page shows
 * another transcript or is left; if that fails, ask before dropping it.
 */
async function saveBeforeLeaving(): Promise<boolean> {
  try {
    await store.flush()
  } catch {
    // reported below via hasUnsavedChanges
  }
  return store.hasUnsavedChanges ? confirmDiscard() : true
}

onBeforeRouteUpdate(async (to, from) => {
  if (to.params.id === from.params.id) return
  return saveBeforeLeaving()
})

onBeforeRouteLeave(() => saveBeforeLeaving())

onMounted(() => {
  void store.load(props.id)
  void termSets.load()
  window.addEventListener('keydown', onKeydown)
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  // reset() still sends pending text (on its own transcript id) before clearing the page state.
  store.reset()
})
</script>

<template>
  <div class="transcript-page">
    <NSpin :show="store.loading">
      <NAlert v-if="store.error" type="error" :title="t('transcript.loadFailed')">
        {{ describe(store.error) }}
        <div class="gap-top">
          <RouterLink :to="{ name: 'jobs' }">
            <NButton size="small">{{ t('transcript.backToJobs') }}</NButton>
          </RouterLink>
        </div>
      </NAlert>

      <NSpace v-else-if="store.transcript" vertical size="large">
        <!-- player -->
        <NCard size="small" class="player-card">
          <template #header>
            <NSpace align="center" size="small">
              <span>{{ t('views.transcript.title') }}</span>
              <NTag size="small">{{ t(`provider.${store.transcript.provider}`) }}</NTag>
              <NText depth="3">
                {{ formatDuration(store.transcript.duration) }}
                · {{ formatDateTime(store.transcript.created_at, locale) }}
              </NText>
            </NSpace>
          </template>
          <template #header-extra>
            <RouterLink :to="{ name: 'jobs' }">
              <NButton size="small" quaternary>{{ t('transcript.backToJobs') }}</NButton>
            </RouterLink>
          </template>
          <NAlert v-if="!hasAudioSource && !store.loading" type="info" :show-icon="false">
            {{ store.audioError ? describe(store.audioError) : t('transcript.noAudio') }}
          </NAlert>
          <template v-else>
            <NAlert v-if="store.audioError" type="warning" :show-icon="false" class="gap">
              {{ t('transcript.audioFailed') }}：{{ describe(store.audioError) }}
            </NAlert>
            <NAlert v-if="playerError" type="warning" :show-icon="false" class="gap">
              {{ t('transcript.audioFailed') }}：{{ playerError }}
            </NAlert>
            <NSpin :show="store.audioLoading" :description="t('transcript.audioLoading')">
              <WaveformPlayer
                ref="player"
                :blob="store.audio"
                :regions="regions"
                @time="(s: number) => (currentTime = s)"
                @playing="(v: boolean) => (playing = v)"
                @error="(m: string) => (playerError = m)"
              />
            </NSpin>
          </template>
          <NSpace class="legend" size="small" data-testid="speaker-legend">
            <span v-for="sp in speakerList" :key="sp.id" class="legend-item">
              <span class="dot" :style="{ background: sp.color }" />
              <span>{{ sp.name }}</span>
            </span>
          </NSpace>
        </NCard>

        <NAlert
          v-if="store.transcript.warnings?.length"
          type="warning"
          :title="t('transcript.warnings')"
        >
          <div v-for="(w, i) in store.transcript.warnings" :key="i">{{ w }}</div>
        </NAlert>

        <div class="workspace">
          <!-- dialogue -->
          <NCard size="small" class="dialogue-card">
            <template #header>
              {{ t('transcript.dialogue', { n: segments.length }) }}
            </template>
            <template #header-extra>
              <NTag size="small" :type="SAVE_TYPE[store.saveState]" :bordered="false" data-testid="save-state">
                {{ t(`proofread.save.${store.saveState}`) }}
              </NTag>
            </template>
            <NSpace class="toolbar" align="center" size="small" wrap>
              <NInput
                ref="searchInput"
                v-model:value="query"
                size="small"
                clearable
                :placeholder="t('proofread.searchPlaceholder')"
                style="width: 200px"
                data-testid="search-input"
                @keydown="onSearchKeydown"
              />
              <NText depth="3" data-testid="search-count">{{ matchCountText }}</NText>
              <NButtonGroup size="small">
                <NButton :disabled="!matches.length" @click="stepMatch(-1)">↑</NButton>
                <NButton :disabled="!matches.length" @click="stepMatch(1)">↓</NButton>
              </NButtonGroup>
              <NInput
                v-model:value="replacement"
                size="small"
                :placeholder="t('proofread.replacePlaceholder')"
                style="width: 160px"
                data-testid="replace-input"
              />
              <NPopconfirm :disabled="!matches.length" @positive-click="replaceAll">
                <template #trigger>
                  <NButton size="small" :disabled="!matches.length" data-testid="replace-all">
                    {{ t('proofread.replaceAll') }}
                  </NButton>
                </template>
                {{ t('proofread.replaceConfirm', { n: matches.length, find: query, replace: replacement }) }}
              </NPopconfirm>
              <NPopconfirm @positive-click="revertAll">
                <template #trigger>
                  <NButton size="small" quaternary type="warning" data-testid="revert">
                    {{ t('proofread.revert') }}
                  </NButton>
                </template>
                {{ t('proofread.revertConfirm') }}
              </NPopconfirm>
            </NSpace>
            <NAlert
              v-if="store.saveState === 'error'"
              :type="saveConflict ? 'warning' : 'error'"
              :show-icon="false"
              class="gap"
              data-testid="save-error"
            >
              <NSpace align="center" justify="space-between" :wrap="false">
                <span>
                  {{
                    saveConflict
                      ? t('proofread.conflictReloaded')
                      : `${t('proofread.saveFailed')}：${describe(store.saveError)}`
                  }}
                </span>
                <NButton size="small" :loading="retrying" data-testid="save-retry" @click="retrySave">
                  {{ t('proofread.retrySave') }}
                </NButton>
              </NSpace>
            </NAlert>
            <NText depth="3" class="hint">{{ t('proofread.shortcuts') }}</NText>
            <NEmpty v-if="!segments.length" :description="t('transcript.empty')" />
            <div v-else ref="listEl" class="dialogue" data-testid="transcript-segments">
              <SegmentRow
                v-for="(seg, i) in segments"
                :key="seg.id"
                :segment="seg"
                :speaker="speakerOf(seg.speaker_id)"
                :speakers="store.transcript.speakers"
                :pieces="piecesFor(seg)"
                :active="seg.id === activeId"
                :editing="seg.id === editingId"
                :is-last="i === segments.length - 1"
                :busy="structuralPending"
                @seek="seekTo(seg)"
                @start-edit="startEdit(seg)"
                @stop-edit="stopEdit"
                @text="(v: string) => store.editText(seg.id, v)"
                @split="(at: number) => split(seg, at)"
                @merge="mergeNext(seg)"
                @set-speaker="(sid: string) => setSpeaker(seg, sid)"
                @new-speaker="newSpeakerFor(seg)"
                @focus-correction="focusCorrection"
              />
            </div>
          </NCard>

          <div class="side">
            <NCard size="small" :title="t('proofread.speakers')">
              <SpeakerPanel
                :speakers="speakerList"
                @rename="renameSpeaker"
                @merge="mergeSpeakers"
                @add="addSpeaker"
              />
            </NCard>
            <NCard size="small" :title="t('summary.title')">
              <SummaryPanel
                :summary="store.transcript.summary ?? null"
                :stale="summaryStale"
                :running="store.summaryRunning"
                @generate="summarize"
                @remove="removeSummary"
              />
            </NCard>
            <NCard size="small" :title="t('proofread.corrections')">
              <CorrectionsPanel
                :corrections="corrections"
                :texts="segmentTexts"
                :term-sets="termSets.items"
                :focused-id="focusedCorrection"
                :focus-seq="focusSeq"
                :llm-running="store.llmRunning"
                @review="review"
                @jump="jumpToSegment"
                @alias="addAlias"
                @llm="suggest"
              />
            </NCard>
            <NCard size="small" :title="t('transcript.export')" data-testid="export-menu">
              <NSpace vertical>
                <NSpace align="center" size="small">
                  <NSwitch v-model:value="exportTimestamps" data-testid="export-timestamps" />
                  <span>{{ t('transcript.includeTimestamps') }}</span>
                </NSpace>
                <NSpace align="center" size="small">
                  <NSwitch v-model:value="exportMerge" data-testid="export-merge" />
                  <span>{{ t('transcript.mergeSameSpeaker') }}</span>
                </NSpace>
                <NButtonGroup>
                  <NButton
                    v-for="fmt in EXPORT_FORMATS"
                    :key="fmt"
                    :loading="store.exporting"
                    :data-testid="`export-${fmt}`"
                    @click="doExport(fmt)"
                  >
                    {{ fmt.toUpperCase() }}
                  </NButton>
                </NButtonGroup>
              </NSpace>
            </NCard>
          </div>
        </div>
      </NSpace>
      <div v-else class="placeholder" />
    </NSpin>
  </div>
</template>

<style scoped>
.transcript-page {
  max-width: 1400px;
  margin: 0 auto;
}

.placeholder {
  min-height: 200px;
}

.gap {
  margin-bottom: 8px;
}

.gap-top {
  margin-top: 8px;
}

.legend {
  margin-top: 12px;
}

.legend-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-right: 12px;
}

.dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  display: inline-block;
}

.workspace {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 360px;
  gap: 16px;
  align-items: start;
}

@media (max-width: 960px) {
  .workspace {
    grid-template-columns: minmax(0, 1fr);
  }
}

.side {
  display: flex;
  flex-direction: column;
  gap: 16px;
  position: sticky;
  top: 8px;
  max-height: calc(100vh - 16px);
  overflow-y: auto;
}

.toolbar {
  margin-bottom: 4px;
}

.hint {
  display: block;
  font-size: 12px;
  margin-bottom: 8px;
}

.dialogue {
  max-height: 65vh;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding-right: 4px;
}
</style>
