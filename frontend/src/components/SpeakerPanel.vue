<script setup lang="ts">
/** Speakers of a transcript: rename (applies everywhere), merge into another, add a new one. */
import { NButton, NInput, NPopselect, NSpace, NText } from 'naive-ui'
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

import type { Speaker } from '@/api'
import { formatDuration } from '@/utils/format'

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

function commit(sp: Speaker, value: string): void {
  const name = value.trim()
  if (name && name !== sp.name) emit('rename', sp.id, name)
}
</script>

<template>
  <div class="speakers" data-testid="speaker-panel">
    <div v-for="sp in speakers" :key="sp.id" class="row" :data-speaker-id="sp.id">
      <span class="dot" :style="{ background: sp.color }" />
      <NInput
        size="small"
        :default-value="sp.name"
        :maxlength="100"
        data-testid="speaker-name"
        @blur="(e: FocusEvent) => commit(sp, (e.target as HTMLInputElement).value)"
        @keydown.enter="(e: KeyboardEvent) => (e.target as HTMLInputElement).blur()"
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
