<script setup lang="ts">
import {
  NAlert,
  NButton,
  NButtonGroup,
  NCard,
  NEmpty,
  NSpace,
  NSpin,
  NSwitch,
  NTag,
  NText,
  useMessage,
} from 'naive-ui'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { RouterLink } from 'vue-router'

import type { ExportFormat, Segment } from '@/api'
import WaveformPlayer, { type WaveRegion } from '@/components/WaveformPlayer.vue'
import { useApiErrors } from '@/composables/useApiErrors'
import { useTranscriptStore } from '@/stores/transcript'
import { formatDateTime, formatDuration, formatTimestamp } from '@/utils/format'
import { findActiveSegment } from '@/utils/segments'

const props = defineProps<{ id: string }>()

const { t, locale } = useI18n()
const message = useMessage()
const { describe, notify } = useApiErrors()
const store = useTranscriptStore()

const player = ref<InstanceType<typeof WaveformPlayer> | null>(null)
const listEl = ref<HTMLElement | null>(null)
const currentTime = ref(0)
const playing = ref(false)
const playerError = ref<string | null>(null)

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

const legend = computed(() => {
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

function speakerName(id: string): string {
  return store.speakerMap.get(id)?.name ?? id
}

function speakerColor(id: string): string {
  return store.speakerMap.get(id)?.color ?? '#888888'
}

function onSegmentClick(seg: Segment): void {
  player.value?.seek(seg.start, true)
  currentTime.value = seg.start
}

function stepSegment(delta: number): void {
  if (!segments.value.length) return
  const base = activeIndex.value >= 0 ? activeIndex.value : findPreviousIndex()
  const next = Math.min(segments.value.length - 1, Math.max(0, base + delta))
  const seg = segments.value[next]
  if (seg) onSegmentClick(seg)
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
  if (!id || !playing.value) return
  await nextTick()
  const el = listEl.value?.querySelector<HTMLElement>(`[data-segment-id="${CSS.escape(id)}"]`)
  el?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
})

// --- keyboard: space = play/pause, ←/→ = previous / next segment ------------------------------

function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  if (target.isContentEditable) return true
  return !!target.closest(
    'input, textarea, select, button, a, [contenteditable], [role="textbox"], [role="button"], [role="switch"], [role="slider"]',
  )
}

function onKeydown(e: KeyboardEvent): void {
  if (e.isComposing || e.ctrlKey || e.metaKey || e.altKey || isTyping(e.target)) return
  if (e.code === 'Space' || e.key === ' ') {
    e.preventDefault()
    player.value?.playPause()
  } else if (e.key === 'ArrowLeft') {
    e.preventDefault()
    stepSegment(-1)
  } else if (e.key === 'ArrowRight') {
    e.preventDefault()
    stepSegment(1)
  }
}

// --- export -----------------------------------------------------------------------------------

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

watch(
  () => props.id,
  (id) => {
    currentTime.value = 0
    playerError.value = null
    void store.load(id)
  },
)

onMounted(() => {
  void store.load(props.id)
  window.addEventListener('keydown', onKeydown)
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
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
            <span v-for="sp in legend" :key="sp.id" class="legend-item">
              <span class="dot" :style="{ background: sp.color }" />
              <span>{{ sp.name }}</span>
              <NText depth="3">
                {{ t('transcript.legendStats', { n: sp.count, time: formatDuration(sp.seconds) }) }}
              </NText>
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

        <!-- export -->
        <NCard size="small" :title="t('transcript.export')" data-testid="export-menu">
          <NSpace align="center">
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

        <!-- dialogue -->
        <NCard size="small" :title="t('transcript.dialogue', { n: segments.length })">
          <NEmpty v-if="!segments.length" :description="t('transcript.empty')" />
          <div v-else ref="listEl" class="dialogue" data-testid="transcript-segments">
            <div
              v-for="seg in segments"
              :key="seg.id"
              class="segment"
              :class="{ active: seg.id === activeId }"
              :style="{ '--speaker-color': speakerColor(seg.speaker_id) }"
              data-testid="transcript-segment"
              :data-segment-id="seg.id"
              :data-active="seg.id === activeId ? 'true' : 'false'"
              @click="onSegmentClick(seg)"
            >
              <div class="segment-meta">
                <span class="speaker-chip">{{ speakerName(seg.speaker_id) }}</span>
                <NText depth="3" class="ts">
                  {{ formatTimestamp(seg.start) }} – {{ formatTimestamp(seg.end) }}
                </NText>
                <NTag v-if="seg.edited" size="tiny" :bordered="false">{{ t('transcript.edited') }}</NTag>
              </div>
              <div class="segment-text">{{ seg.text }}</div>
            </div>
          </div>
        </NCard>
      </NSpace>
      <div v-else class="placeholder" />
    </NSpin>
  </div>
</template>

<style scoped>
.transcript-page {
  max-width: 1200px;
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

.dialogue {
  max-height: 60vh;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding-right: 4px;
}

.segment {
  border-left: 3px solid var(--speaker-color);
  padding: 6px 10px;
  border-radius: 4px;
  cursor: pointer;
  transition: background-color 0.15s;
}

.segment:hover {
  background-color: rgba(128, 128, 128, 0.08);
}

.segment.active {
  background-color: color-mix(in srgb, var(--speaker-color) 18%, transparent);
}

.segment-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 2px;
}

.speaker-chip {
  background: var(--speaker-color);
  color: #fff;
  border-radius: 10px;
  padding: 0 8px;
  font-size: 12px;
  line-height: 20px;
  font-weight: 500;
  text-shadow: 0 0 2px rgba(0, 0, 0, 0.35);
}

.ts {
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.segment-text {
  line-height: 1.7;
  white-space: pre-wrap;
}
</style>
