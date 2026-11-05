<script setup lang="ts">
/**
 * Timestamp chip after a cited summary item. One source: click jumps to it. Several: a tiny menu
 * (time · speaker · first words) picks one. Renders nothing without resolvable sources.
 */
import { NDropdown, type DropdownOption } from 'naive-ui'
import { computed, type HTMLAttributes } from 'vue'
import { useI18n } from 'vue-i18n'

import { formatTimestamp } from '@/utils/format'
import type { ResolvedRef } from '@/utils/summary'

const props = defineProps<{
  targets: ResolvedRef[]
  speakerName: (speakerId: string) => string
}>()

const emit = defineEmits<{ jump: [target: ResolvedRef] }>()

const { t } = useI18n()

const SNIPPET = 24

function snippet(text: string): string {
  const chars = [...text.trim()]
  return chars.length > SNIPPET ? `${chars.slice(0, SNIPPET).join('')}…` : chars.join('')
}

const first = computed(() => props.targets[0] ?? null)
const label = computed(() => (first.value ? formatTimestamp(first.value.start) : ''))
const title = computed(() =>
  props.targets.length > 1
    ? t('summary.refsTitle', { n: props.targets.length })
    : t('summary.refTitle', { time: label.value }),
)

const options = computed<DropdownOption[]>(() =>
  props.targets.map((r, i) => ({
    key: i,
    label: t('summary.refOption', {
      time: formatTimestamp(r.start),
      speaker: props.speakerName(r.speakerId),
      text: snippet(r.text),
    }),
    props: { 'data-testid': 'summary-ref-option' } as HTMLAttributes,
  })),
)

function onSelect(key: string | number): void {
  const target = props.targets[Number(key)]
  if (target) emit('jump', target)
}
</script>

<template>
  <NDropdown v-if="targets.length > 1" trigger="click" size="small" :options="options" @select="onSelect">
    <button type="button" class="ref-chip" :title="title" :aria-label="title" data-testid="summary-ref">
      {{ label }}<span class="more">+{{ targets.length - 1 }}</span>
    </button>
  </NDropdown>
  <button
    v-else-if="first"
    type="button"
    class="ref-chip"
    :title="title"
    :aria-label="title"
    data-testid="summary-ref"
    @click="emit('jump', first)"
  >
    {{ label }}
  </button>
</template>

<style scoped>
.ref-chip {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  margin-left: 6px;
  padding: 0 6px;
  border: 1px solid rgba(32, 128, 240, 0.35);
  border-radius: 9px;
  background: rgba(32, 128, 240, 0.08);
  color: #2080f0;
  font: inherit;
  font-size: 11px;
  line-height: 16px;
  font-variant-numeric: tabular-nums;
  vertical-align: 1px;
  cursor: pointer;
}

.ref-chip:hover {
  background: rgba(32, 128, 240, 0.16);
}

.ref-chip:focus-visible {
  outline: 2px solid #2080f0;
  outline-offset: 1px;
}

.more {
  opacity: 0.7;
}
</style>
