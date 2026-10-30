<script setup lang="ts">
/**
 * M11 LLM meeting summary: overview, key points, decisions, action items. Generating costs money
 * and sends the text to DashScope, so it sits behind a confirmation like the LLM suggestions.
 */
import { NAlert, NButton, NEmpty, NPopconfirm, NSpace, NText } from 'naive-ui'
import { useI18n } from 'vue-i18n'

import type { ActionItem, TranscriptSummary } from '@/api'
import { formatDateTime } from '@/utils/format'

defineProps<{
  summary: TranscriptSummary | null
  stale: boolean
  running: boolean
}>()

const emit = defineEmits<{ generate: []; remove: [] }>()

const { t, locale } = useI18n()

/** "Owner: X · Due: Y" with the locale's punctuation. */
function whoLine(a: ActionItem): string {
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
      <section v-if="summary.key_points?.length">
        <h4>{{ t('summary.keyPoints') }}</h4>
        <ul>
          <li v-for="(p, i) in summary.key_points" :key="i">{{ p }}</li>
        </ul>
      </section>
      <section v-if="summary.decisions?.length">
        <h4>{{ t('summary.decisions') }}</h4>
        <ul>
          <li v-for="(d, i) in summary.decisions" :key="i">{{ d }}</li>
        </ul>
      </section>
      <section v-if="summary.action_items?.length">
        <h4>{{ t('summary.actionItems') }}</h4>
        <ul class="actions" data-testid="summary-actions">
          <li v-for="(a, i) in summary.action_items" :key="i">
            <span>{{ a.task }}</span>
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
