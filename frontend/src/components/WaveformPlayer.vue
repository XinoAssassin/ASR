<script setup lang="ts">
/**
 * wavesurfer.js waveform + transport controls. Audio arrives as a Blob (fetched with the API key
 * header, D6); one coloured region per transcript segment (regions plugin, read-only).
 */
import { NButton, NSlider, NSpace, NText } from 'naive-ui'
import { onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import WaveSurfer from 'wavesurfer.js'
import RegionsPlugin from 'wavesurfer.js/dist/plugins/regions.esm.js'

import { formatClock } from '@/utils/format'

export interface WaveRegion {
  id: string
  start: number
  end: number
  color: string
}

const props = defineProps<{ blob: Blob | null; regions: WaveRegion[] }>()
const emit = defineEmits<{
  time: [seconds: number]
  playing: [value: boolean]
  ready: [duration: number]
  error: [message: string]
}>()

const { t } = useI18n()

const container = ref<HTMLDivElement | null>(null)
const ws = shallowRef<WaveSurfer | null>(null)
const regionsPlugin = shallowRef<InstanceType<typeof RegionsPlugin> | null>(null)
const ready = ref(false)
const loading = ref(false)
const playing = ref(false)
const currentTime = ref(0)
const duration = ref(0)
const zoom = ref(0)
/** The Blob currently loaded (null = nothing); `ready` events without one are ignored. */
let loadedBlob: Blob | null = null
/** A seek requested before the audio was ready; applied on `ready`. */
let pendingSeek: { seconds: number; play: boolean } | null = null

/** "#RRGGBB" → translucent fill; other CSS colours are used as-is. */
function fill(color: string): string {
  return /^#[0-9a-f]{6}$/i.test(color) ? `${color}40` : color
}

function renderRegions(): void {
  const plugin = regionsPlugin.value
  if (!plugin || !ready.value) return
  plugin.clearRegions()
  for (const r of props.regions) {
    if (!(r.end > r.start)) continue
    plugin.addRegion({
      id: r.id,
      start: r.start,
      end: r.end,
      color: fill(r.color),
      drag: false,
      resize: false,
    })
  }
}

async function load(blob: Blob | null): Promise<void> {
  const instance = ws.value
  if (!instance) return
  ready.value = false
  duration.value = 0
  currentTime.value = 0
  const previous = loadedBlob
  loadedBlob = blob
  if (!blob) {
    // empty() emits a zero-length `ready`; only needed when something was loaded before.
    if (previous) instance.empty()
    return
  }
  loading.value = true
  try {
    await instance.loadBlob(blob)
  } catch (e) {
    if (!(e instanceof DOMException && e.name === 'AbortError')) {
      emit('error', e instanceof Error ? e.message : String(e))
    }
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  if (!container.value) return
  const plugin = RegionsPlugin.create()
  const instance = WaveSurfer.create({
    container: container.value,
    height: 96,
    waveColor: '#a3a3a3',
    progressColor: '#18a058',
    cursorColor: '#18a058',
    cursorWidth: 2,
    normalize: true,
    dragToSeek: true,
    autoScroll: true,
    autoCenter: true,
    plugins: [plugin],
  })
  instance.on('ready', (d) => {
    if (!loadedBlob) return
    duration.value = d
    ready.value = true
    renderRegions()
    emit('ready', d)
    if (pendingSeek) {
      const { seconds, play } = pendingSeek
      pendingSeek = null
      seek(seconds, play)
    }
  })
  instance.on('timeupdate', (time) => {
    currentTime.value = time
    emit('time', time)
  })
  instance.on('play', () => {
    playing.value = true
    emit('playing', true)
  })
  instance.on('pause', () => {
    playing.value = false
    emit('playing', false)
  })
  instance.on('finish', () => {
    playing.value = false
    emit('playing', false)
  })
  instance.on('error', (err) => emit('error', err.message))
  plugin.on('region-clicked', (region, e) => {
    e.stopPropagation()
    seek(region.start, true)
  })
  ws.value = instance
  regionsPlugin.value = plugin
  void load(props.blob)
})

onBeforeUnmount(() => {
  ws.value?.destroy()
  ws.value = null
  regionsPlugin.value = null
})

watch(
  () => props.blob,
  (blob) => void load(blob),
)
watch(() => props.regions, renderRegions)
watch(zoom, (v) => {
  if (ready.value) ws.value?.zoom(v)
})

function seek(seconds: number, play = false): void {
  const instance = ws.value
  if (!instance || !ready.value) {
    pendingSeek = { seconds, play }
    return
  }
  instance.setTime(Math.max(0, Math.min(seconds, duration.value || seconds)))
  if (play && !instance.isPlaying()) void instance.play()
}

function playPause(): void {
  if (ws.value && ready.value) void ws.value.playPause()
}

defineExpose({ seek, playPause, isReady: () => ready.value })
</script>

<template>
  <div class="player" data-testid="waveform-player" :data-ready="ready ? 'true' : 'false'">
    <div ref="container" class="waveform" data-testid="waveform" />
    <NSpace align="center" justify="space-between" class="controls">
      <NSpace align="center">
        <NButton
          type="primary"
          :disabled="!ready"
          :loading="loading"
          data-testid="play-pause"
          @click="playPause"
        >
          {{ playing ? t('transcript.pause') : t('transcript.play') }}
        </NButton>
        <NText code data-testid="player-time">
          {{ formatClock(currentTime) }} / {{ formatClock(duration) }}
        </NText>
        <NText depth="3" class="shortcut">{{ t('transcript.shortcuts') }}</NText>
      </NSpace>
      <NSpace align="center" :wrap="false">
        <NText depth="3">{{ t('transcript.zoom') }}</NText>
        <NSlider v-model:value="zoom" :min="0" :max="200" :step="10" :disabled="!ready" style="width: 160px" />
      </NSpace>
    </NSpace>
  </div>
</template>

<style scoped>
.waveform {
  width: 100%;
  min-height: 96px;
}

.controls {
  margin-top: 8px;
}

.shortcut {
  font-size: 12px;
}
</style>
