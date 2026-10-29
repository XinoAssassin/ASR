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
  useMessage,
} from 'naive-ui'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { RouterLink, onBeforeRouteLeave } from 'vue-router'

import type { Correction, ExportFormat, Segment, Speaker } from '@/api'
import CorrectionsPanel from '@/components/CorrectionsPanel.vue'
import SegmentRow from '@/components/SegmentRow.vue'
import SpeakerPanel from '@/components/SpeakerPanel.vue'
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

function speakerOf(id: string): Speaker {
  return store.speakerMap.get(id) ?? { id, name: id, color: '#888888' }
}

// --- search / replace ---------------------------------------------------------------------------

const query = ref('')
const replacement = ref('')
const matchIndex = ref(0)
const matches = computed(() => findMatches(segments.value, query.value))
const currentMatch = computed(() => matches.value[matchIndex.value] ?? null)

watch(query, () => (matchIndex.value = 0))

function stepMatch(delta: number): void {
  if (!matches.value.length) return
  matchIndex.value = (matchIndex.value + delta + matches.value.length) % matches.value.length
  const m = currentMatch.value
  if (m) void scrollTo(m.segmentId)
}

async function replaceAll(): Promise<void> {
  const find = query.value.trim()
  if (!find) return
  const n = matches.value.length
  try {
    await store.applyOps([{ op: 'replace', find, replace: replacement.value }])
    message.success(t('proofread.replaced', { n }))
  } catch (e) {
    notify(e, t('proofread.saveFailed'))
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

async function run(op: () => Promise<void>): Promise<void> {
  try {
    await op()
  } catch (e) {
    notify(e, t('proofread.saveFailed'))
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

function split(seg: Segment, at: number): void {
  editingId.value = null
  void run(() => store.applyOps([{ op: 'split', segment_id: seg.id, at }]))
}

function mergeNext(seg: Segment): void {
  editingId.value = null
  void run(() => store.applyOps([{ op: 'merge', segment_id: seg.id }]))
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
}

async function addAlias(c: Correction, termSetId: string | null): Promise<void> {
  try {
    const set = await termSets.addAlias(termSetId, c.after, c.before, t('proofread.defaultSetName'))
    message.success(t('proofread.aliasAdded', { alias: c.before, term: c.after, set: set.name }))
  } catch (e) {
    notify(e, t('proofread.aliasFailed'))
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

function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  if (target.isContentEditable) return true
  return !!target.closest(
    'input, textarea, select, button, a, [contenteditable], [role="textbox"], [role="button"], [role="switch"], [role="slider"]',
  )
}

function onKeydown(e: KeyboardEvent): void {
  if (e.isComposing) return
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'f') {
    e.preventDefault()
    searchInput.value?.focus()
    return
  }
  if (e.ctrlKey || e.metaKey || e.altKey || isTyping(e.target)) return
  if (e.code === 'Space' || e.key === ' ') {
    e.preventDefault()
    player.value?.playPause()
  } else if (e.key === 'ArrowLeft') {
    e.preventDefault()
    stepSegment(-1)
  } else if (e.key === 'ArrowRight') {
    e.preventDefault()
    stepSegment(1)
  } else if (e.key === 'Enter') {
    const seg = segments.value[activeIndex.value >= 0 ? activeIndex.value : 0]
    if (seg) {
      e.preventDefault()
      startEdit(seg)
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
    void store.load(id)
  },
)

onBeforeRouteLeave(async () => {
  await store.flush().catch(() => undefined)
})

onMounted(() => {
  void store.load(props.id)
  void termSets.load()
  window.addEventListener('keydown', onKeydown)
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  void store.flush().catch(() => undefined)
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
                @keydown.enter="stepMatch(1)"
              />
              <NText depth="3" data-testid="search-count">
                {{ matches.length ? `${matchIndex + 1}/${matches.length}` : '0/0' }}
              </NText>
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
            <NCard size="small" :title="t('proofread.corrections')">
              <CorrectionsPanel
                :corrections="corrections"
                :term-sets="termSets.items"
                :focused-id="focusedCorrection"
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
