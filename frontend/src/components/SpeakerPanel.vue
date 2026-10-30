<script setup lang="ts">
/**
 * Speakers of a transcript: rename (applies everywhere), merge into another, add a new one.
 * Name inputs are controlled: a draft exists only while a row is being edited and is dropped on
 * blur, so the field always falls back to the saved `sp.name` (after a save, a failed save, or a
 * rename that came from elsewhere).
 */
import { NButton, NInput, NPopselect, NSpace, NText } from 'naive-ui'
import { computed, reactive } from 'vue'
import { useI18n } from 'vue-i18n'

import type { Speaker } from '@/api'
import { formatDuration } from '@/utils/format'

import { isImeKey } from './interaction'

const props = defineProps<{
  speakers: (Speaker & { count: number; seconds: number })[]
}>()

const emit = defineEmits<{
  rename: [id: string, name: string]
  merge: [fromId: string, intoId: string]
  add: []
}>()

const { t } = useI18n()

function targets(id: string) {
  return props.speakers.filter((s) => s.id !== id).map((s) => ({ label: s.name, value: s.id }))
}

const canMerge = computed(() => props.speakers.length > 1)

/** Unsaved name per speaker id, only while its input is being edited. */
const drafts = reactive<Record<string, string>>({})

function nameOf(sp: Speaker): string {
  return drafts[sp.id] ?? sp.name
}

function onInput(sp: Speaker, value: string): void {
  drafts[sp.id] = value
}

function commit(sp: Speaker): void {
  const draft = drafts[sp.id]
  delete drafts[sp.id] // show sp.name again; it updates once the rename is saved
  const name = draft?.trim()
  if (name && name !== sp.name) emit('rename', sp.id, name)
}

function onKeydown(sp: Speaker, e: KeyboardEvent): void {
  if (isImeKey(e)) return // Enter / Esc that confirm or cancel an IME candidate
  if (e.key === 'Escape') delete drafts[sp.id] // discard; the blur below then commits nothing
  if (e.key === 'Enter' || e.key === 'Escape') {
    e.preventDefault()
    ;(e.target as HTMLInputElement).blur()
  }
}
</script>

<template>
  <div class="speakers" data-testid="speaker-panel">
    <div v-for="sp in speakers" :key="sp.id" class="row" :data-speaker-id="sp.id">
      <span class="dot" :style="{ background: sp.color }" />
      <NInput
        size="small"
        :value="nameOf(sp)"
        :maxlength="100"
        data-testid="speaker-name"
        @update:value="(v: string) => onInput(sp, v)"
        @blur="commit(sp)"
        @keydown="(e: KeyboardEvent) => onKeydown(sp, e)"
      />
      <NText depth="3" class="stats">
        {{ t('transcript.legendStats', { n: sp.count, time: formatDuration(sp.seconds) }) }}
      </NText>
      <NPopselect
        v-if="canMerge"
        :options="targets(sp.id)"
        trigger="click"
        @update:value="(into: string) => emit('merge', sp.id, into)"
      >
        <NButton size="tiny" quaternary data-testid="speaker-merge">{{ t('proofread.mergeInto') }}</NButton>
      </NPopselect>
    </div>
    <NSpace>
      <NButton size="small" dashed data-testid="speaker-add" @click="emit('add')">
        {{ t('proofread.addSpeaker') }}
      </NButton>
    </NSpace>
  </div>
</template>

<style scoped>
.speakers {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.row {
  display: grid;
  grid-template-columns: 12px 1fr auto auto;
  align-items: center;
  gap: 8px;
}

.dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
}

.stats {
  font-size: 12px;
  white-space: nowrap;
}
</style>
