<script setup lang="ts">
/**
 * One transcript segment for proofreading: click to seek, Enter / double-click to edit (autosaved
 * by the store), split at the caret, merge with the next segment, reassign the speaker.
 * Highlights are rendered as plain spans from `pieces` (no v-html). While a split / merge is in
 * flight (`busy`) both are ignored, so a double-click on "merge" cannot merge twice.
 */
import { NButton, NDropdown, NInput, NTag, NText, type DropdownOption } from 'naive-ui'
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import type { Segment, Speaker } from '@/api'
import { formatTimestamp } from '@/utils/format'
import type { Piece } from '@/utils/highlight'
import { isImeKey, splitOffset } from './interaction'

const props = defineProps<{
  segment: Segment
  speaker: Speaker
  speakers: Speaker[]
  pieces: Piece[]
  active: boolean
  editing: boolean
  isLast: boolean
  /** A split / merge is pending: ignore further ones until it settles. */
  busy?: boolean
}>()

const emit = defineEmits<{
  seek: []
  startEdit: []
  stopEdit: []
  text: [value: string]
  /** `at` is a code-point offset into the text (the backend counts code points, not UTF-16). */
  split: [at: number]
  merge: []
  setSpeaker: [speakerId: string]
  newSpeaker: []
  focusCorrection: [id: string]
}>()

const { t } = useI18n()
const input = ref<InstanceType<typeof NInput> | null>(null)
const draft = ref(props.segment.text)

watch(
  () => props.segment.text,
  (text) => {
    if (!props.editing) draft.value = text
  },
)

watch(
  () => props.editing,
  async (on) => {
    if (!on) return
    draft.value = props.segment.text
    await nextTick()
    input.value?.focus()
  },
)

const speakerOptions = computed<DropdownOption[]>(() => [
  ...props.speakers.map((s) => ({ label: s.name, key: s.id, disabled: s.id === props.speaker.id })),
  { type: 'divider', key: 'd' },
  { label: t('proofread.newSpeaker'), key: '__new__' },
])

function onSpeaker(key: string | number): void {
  if (key === '__new__') emit('newSpeaker')
  else emit('setSpeaker', String(key))
}

function onInput(value: string): void {
  draft.value = value
  emit('text', value)
}

function splitHere(): void {
  if (props.busy) return
  const at = splitOffset(draft.value, input.value?.textareaElRef?.selectionStart)
  if (at !== null) emit('split', at)
}

function mergeNext(): void {
  if (!props.busy) emit('merge')
}

function onKeydown(e: KeyboardEvent): void {
  if (isImeKey(e)) return
  if (e.key === 'Escape') {
    e.preventDefault()
    emit('stopEdit')
  } else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault()
    splitHere()
  }
}

function pieceClass(p: Piece): string[] {
  return p.kinds.map((k) => `mark-${k}`)
}

function onPieceClick(p: Piece, e: MouseEvent): void {
  const ref = p.refs[0]
  if (ref) {
    e.stopPropagation()
    emit('focusCorrection', ref)
  }
}
</script>

<template>
  <div
    class="segment"
    :class="{ active, editing }"
    :style="{ '--speaker-color': speaker.color }"
    data-testid="transcript-segment"
    :data-segment-id="segment.id"
    :data-active="active ? 'true' : 'false'"
    @click="!editing && emit('seek')"
    @dblclick="emit('startEdit')"
  >
    <div class="segment-meta">
      <NDropdown trigger="click" :options="speakerOptions" @select="onSpeaker">
        <span class="speaker-chip" data-testid="segment-speaker" @click.stop @dblclick.stop>{{
          speaker.name
        }}</span>
      </NDropdown>
      <NText depth="3" class="ts">
        {{ formatTimestamp(segment.start) }} – {{ formatTimestamp(segment.end) }}
      </NText>
      <NTag v-if="segment.edited" size="tiny" :bordered="false">{{ t('transcript.edited') }}</NTag>
      <span class="actions" @click.stop @dblclick.stop>
        <NButton v-if="!editing" size="tiny" quaternary data-testid="segment-edit" @click="emit('startEdit')">
          {{ t('proofread.edit') }}
        </NButton>
        <template v-else>
          <NButton
            size="tiny"
            quaternary
            :disabled="busy"
            data-testid="segment-split"
            @click="splitHere"
          >
            {{ t('proofread.split') }}
          </NButton>
          <NButton size="tiny" quaternary data-testid="segment-done" @click="emit('stopEdit')">
            {{ t('proofread.done') }}
          </NButton>
        </template>
        <NButton
          v-if="!isLast"
          size="tiny"
          quaternary
          :disabled="busy"
          data-testid="segment-merge"
          @click="mergeNext"
        >
          {{ t('proofread.mergeNext') }}
        </NButton>
      </span>
    </div>
    <NInput
      v-if="editing"
      ref="input"
      type="textarea"
      :value="draft"
      :autosize="{ minRows: 1, maxRows: 8 }"
      data-testid="segment-input"
      @update:value="onInput"
      @keydown="onKeydown"
      @click.stop
    />
    <div v-else class="segment-text" data-testid="segment-text">
      <span
        v-for="(p, i) in pieces"
        :key="i"
        :class="pieceClass(p)"
        @click="onPieceClick(p, $event)"
      >{{ p.text }}</span>
    </div>
  </div>
</template>

<style scoped>
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

.segment.editing {
  cursor: default;
  background-color: rgba(128, 128, 128, 0.06);
}

.segment-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 2px;
}

.actions {
  margin-left: auto;
  opacity: 0;
  transition: opacity 0.15s;
}

.segment:hover .actions,
.segment.editing .actions,
.segment.active .actions {
  opacity: 1;
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
  cursor: pointer;
}

.ts {
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.segment-text {
  line-height: 1.7;
  white-space: pre-wrap;
}

.mark-term {
  text-decoration: underline dotted;
  text-decoration-color: #2080f0;
  text-underline-offset: 3px;
}

.mark-correction {
  background: rgba(24, 160, 88, 0.18);
  border-radius: 2px;
  cursor: help;
}

.mark-suggestion {
  background: rgba(240, 160, 32, 0.28);
  border-radius: 2px;
  cursor: help;
}

.mark-search {
  background: rgba(255, 214, 0, 0.55);
}

.mark-current {
  outline: 2px solid #f0a020;
}
</style>
