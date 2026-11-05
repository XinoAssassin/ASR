<script setup lang="ts">
/**
 * M11 LLM meeting summary: overview, key points, decisions, action items. Generating costs money
 * and sends the text to DashScope, so it sits behind a confirmation like the LLM suggestions.
 *
 * Cited items (`refs`, segment ids) get a timestamp chip (`SummaryRefChip`): one source jumps to it
 * directly, several open a tiny menu. The parent resolves refs against the current
 * segments (stale ids fall back to `original`, unresolvable ones are skipped) and does the jump.
 * Summaries written before citations existed (no refs / plain strings) render as plain lists.
 */
import { NAlert, NButton, NEmpty, NPopconfirm, NSpace, NText } from 'naive-ui'
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

import type { TranscriptSummary } from '@/api'
import { formatDateTime } from '@/utils/format'
import { summaryActions, summaryEntries, type ResolvedRef, type SummaryActionEntry } from '@/utils/summary'

import SummaryRefChip from './SummaryRefChip.vue'

const props = withDefaults(
  defineProps<{
    summary: TranscriptSummary | null
    stale: boolean
    running: boolean
    /** Cited segment ids → where they point now (skips the ones that no longer resolve). */
    resolveRefs?: (refs: string[]) => ResolvedRef[]
    speakerName?: (speakerId: string) => string
  }>(),
  { resolveRefs: () => [], speakerName: (id: string) => id },
)

const emit = defineEmits<{ generate: []; remove: []; jump: [target: ResolvedRef] }>()

const { t, locale } = useI18n()

const groups = computed(() =>
  (
    [
      { key: 'keyPoints', testid: 'summary-key-points', items: summaryEntries(props.summary?.key_points) },
      { key: 'decisions', testid: 'summary-decisions', items: summaryEntries(props.summary?.decisions) },
    ] as const
  )
    .map((g) => ({ ...g, items: g.items.map((e) => ({ ...e, targets: props.resolveRefs(e.refs) })) }))
    .filter((g) => g.items.length),
)
const actions = computed(() =>
  summaryActions(props.summary?.action_items).map((a) => ({ ...a, targets: props.resolveRefs(a.refs) })),
)

/** "Owner: X · Due: Y" with the locale's punctuation. */
function whoLine(a: SummaryActionEntry): string {
  return [
    a.owner && t('common.labelDetail', { label: t('summary.owner'), detail: a.owner }),
    a.due && t('common.labelDetail', { label: t('summary.due'), detail: a.due }),
  ]
    .filter(Boolean)
    .join(' · ')
}
</script>

<template>
  <div class="summary" data-testid="summary-panel">
    <NSpace justify="space-between" align="center">
      <NPopconfirm @positive-click="emit('generate')">
        <template #trigger>
          <NButton size="small" :loading="running" data-testid="summary-generate">
            {{ summary ? t('summary.regenerate') : t('summary.generate') }}
          </NButton>
        </template>
        {{ t('summary.confirm') }}
      </NPopconfirm>
      <NPopconfirm v-if="summary" @positive-click="emit('remove')">
        <template #trigger>
          <NButton size="small" quaternary data-testid="summary-remove">{{ t('summary.remove') }}</NButton>
        </template>
        {{ t('summary.removeConfirm') }}
      </NPopconfirm>
    </NSpace>

    <NText v-if="running" depth="3">{{ t('summary.running') }}</NText>
    <NEmpty v-else-if="!summary" size="small" :description="t('summary.empty')" />
    <template v-else>
      <NAlert v-if="stale" type="warning" :show-icon="false" data-testid="summary-stale">
        {{ t('summary.stale') }}
      </NAlert>
      <section>
        <h4>{{ t('summary.overview') }}</h4>
        <p data-testid="summary-overview">{{ summary.overview }}</p>
      </section>
      <section v-for="group in groups" :key="group.key">
        <h4>{{ t(`summary.${group.key}`) }}</h4>
        <ul :data-testid="group.testid">
          <li v-for="(p, i) in group.items" :key="i" data-testid="summary-item">
            <span>{{ p.text }}</span>
            <SummaryRefChip :targets="p.targets" :speaker-name="speakerName" @jump="(r) => emit('jump', r)" />
          </li>
        </ul>
      </section>
      <section v-if="actions.length">
        <h4>{{ t('summary.actionItems') }}</h4>
        <ul class="actions" data-testid="summary-actions">
          <li v-for="(a, i) in actions" :key="i" data-testid="summary-item">
            <span>{{ a.task }}</span>
            <SummaryRefChip :targets="a.targets" :speaker-name="speakerName" @jump="(r) => emit('jump', r)" />
            <NText v-if="a.owner || a.due" depth="3" class="who">
              {{ whoLine(a) }}
            </NText>
          </li>
        </ul>
      </section>
      <NText depth="3" class="meta">
        {{ t('summary.meta', { model: summary.model, time: formatDateTime(summary.created_at, locale) }) }}
      </NText>
    </template>
  </div>
</template>

<style scoped>
.summary {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

h4 {
  margin: 4px 0 2px;
  font-size: 13px;
}

p,
ul {
  margin: 0;
  line-height: 1.6;
}

ul {
  padding-left: 18px;
}

.who {
  display: block;
  font-size: 12px;
}

.meta {
  font-size: 12px;
}
</style>
