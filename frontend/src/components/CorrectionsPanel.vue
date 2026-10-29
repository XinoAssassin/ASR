<script setup lang="ts">
/**
 * [5b] corrections for review: automatic ones (term / repetition) and LLM suggestions. Accept /
 * reject, jump to the segment, add the mis-hearing as an alias to a term set, and ask the LLM.
 */
import { NButton, NEmpty, NPopconfirm, NRadioButton, NRadioGroup, NSelect, NSpace, NTag, NText } from 'naive-ui'
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import type { Correction, TermSetOut } from '@/api'
import { correctionContext } from '@/utils/highlight'

const props = defineProps<{
  corrections: Correction[]
  /** Current text per segment id, for the context line. */
  texts: Record<string, string>
  termSets: TermSetOut[]
  focusedId: string | null
  llmRunning: boolean
}>()

const emit = defineEmits<{
  review: [id: string, decision: 'accept' | 'reject']
  jump: [segmentId: string]
  alias: [correction: Correction, termSetId: string | null]
  llm: []
}>()

const { t } = useI18n()
const filter = ref<'open' | 'all'>('open')
const aliasSet = ref<string | null>(null)

const OPEN = new Set(['applied', 'suggested'])
const visible = computed(() =>
  props.corrections.filter((c) => filter.value === 'all' || OPEN.has(c.status ?? 'applied')),
)
const openCount = computed(() => props.corrections.filter((c) => OPEN.has(c.status ?? 'applied')).length)
const setOptions = computed(() => [
  ...props.termSets.map((s) => ({ label: s.name, value: s.id })),
  { label: t('proofread.newTermSet'), value: '__new__' },
])

const contexts = computed(() => {
  const out = new Map<Correction, ReturnType<typeof correctionContext>>()
  for (const c of visible.value) {
    const text = props.texts[c.segment_id]
    out.set(c, text === undefined ? null : correctionContext(text, c))
  }
  return out
})

const STATUS_TYPE = { applied: 'success', suggested: 'warning', accepted: 'info', rejected: 'default' } as const
</script>

<template>
  <div class="corrections" data-testid="corrections-panel">
    <NSpace justify="space-between" align="center" class="head">
      <NRadioGroup v-model:value="filter" size="small">
        <NRadioButton value="open">{{ t('proofread.openCorrections', { n: openCount }) }}</NRadioButton>
        <NRadioButton value="all">{{ t('proofread.allCorrections') }}</NRadioButton>
      </NRadioGroup>
      <NPopconfirm @positive-click="emit('llm')">
        <template #trigger>
          <NButton size="small" :loading="llmRunning" data-testid="llm-suggest">
            {{ t('proofread.llmSuggest') }}
          </NButton>
        </template>
        {{ t('proofread.llmConfirm') }}
      </NPopconfirm>
    </NSpace>
    <NSpace align="center" size="small" class="alias-target">
      <NText depth="3">{{ t('proofread.aliasTarget') }}</NText>
      <NSelect
        v-model:value="aliasSet"
        size="small"
        :options="setOptions"
        :placeholder="t('proofread.chooseTermSet')"
        style="width: 180px"
        data-testid="alias-term-set"
      />
    </NSpace>
    <NEmpty v-if="!visible.length" size="small" :description="t('proofread.noCorrections')" />
    <div
      v-for="c in visible"
      :key="c.id"
      class="item"
      :class="{ focused: c.id === focusedId }"
      data-testid="correction-item"
      :data-correction-id="c.id"
      :data-status="c.status"
    >
      <div class="change" @click="emit('jump', c.segment_id)">
        <del>{{ c.before || '∅' }}</del>
        <span class="arrow">→</span>
        <ins>{{ c.after || '∅' }}</ins>
      </div>
      <NText v-if="contexts.get(c)" depth="3" class="context" data-testid="correction-context">
        {{ contexts.get(c)!.left }}<span class="hit">{{ contexts.get(c)!.hit || '▾' }}</span>{{ contexts.get(c)!.right }}
      </NText>
      <NSpace size="small" align="center">
        <NTag size="tiny" :type="STATUS_TYPE[c.status ?? 'applied']" :bordered="false">
          {{ t(`proofread.source.${c.source}`) }} · {{ t(`proofread.status.${c.status ?? 'applied'}`) }}
        </NTag>
        <template v-if="OPEN.has(c.status ?? 'applied')">
          <NButton size="tiny" type="primary" ghost data-testid="correction-accept" @click="emit('review', c.id, 'accept')">
            {{ t('proofread.accept') }}
          </NButton>
          <NButton size="tiny" data-testid="correction-reject" @click="emit('review', c.id, 'reject')">
            {{ t('proofread.reject') }}
          </NButton>
        </template>
        <NButton
          v-if="c.before && c.after && c.source !== 'repetition'"
          size="tiny"
          quaternary
          :disabled="!aliasSet"
          data-testid="correction-alias"
          @click="emit('alias', c, aliasSet === '__new__' ? null : aliasSet)"
        >
          {{ t('proofread.addAlias') }}
        </NButton>
      </NSpace>
    </div>
  </div>
</template>

<style scoped>
.corrections {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.item {
  border: 1px solid rgba(128, 128, 128, 0.25);
  border-radius: 6px;
  padding: 6px 8px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.item.focused {
  border-color: #f0a020;
  box-shadow: 0 0 0 2px rgba(240, 160, 32, 0.25);
}

.change {
  cursor: pointer;
}

del {
  color: #d03050;
}

ins {
  color: #18a058;
  text-decoration: none;
  font-weight: 500;
}

.context {
  font-size: 12px;
  line-height: 1.5;
}

.hit {
  background: rgba(240, 160, 32, 0.25);
  border-radius: 2px;
  padding: 0 1px;
}

.arrow {
  margin: 0 6px;
  opacity: 0.6;
}
</style>
