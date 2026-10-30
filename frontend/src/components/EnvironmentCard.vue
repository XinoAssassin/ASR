<script setup lang="ts">
/**
 * Read-only server facts that only environment variables set (ARCH_REVIEW §4-f): auth, CORS,
 * hosts, upload roots, directories, decoder, log settings. Changed in the env / `.env` plus a
 * restart; `PUT /api/settings` cannot touch them. No secrets are in `environment`.
 */
import { NCard, NDescriptions, NDescriptionsItem, NText } from 'naive-ui'
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

import type { EnvironmentOut } from '@/api'
import { envFactRows } from '@/utils/envFacts'

const props = defineProps<{ environment: EnvironmentOut; envOnly: Record<string, string> }>()

const { t, te } = useI18n()

const rows = computed(() =>
  envFactRows(props.environment, props.envOnly).map((row) => ({
    ...row,
    label: te(`envFacts.${row.labelKey}`) ? t(`envFacts.${row.labelKey}`) : row.key,
    text:
      row.value.kind === 'bool'
        ? t(row.value.value ? 'common.yes' : 'common.no')
        : row.value.kind === 'auto'
          ? t('envFacts.auto')
          : row.value.kind === 'none'
            ? t('envFacts.none')
            : row.value.kind === 'list'
              ? row.value.value.join(t('common.listSep'))
              : row.value.value,
  })),
)
</script>

<template>
  <NCard :title="t('envFacts.title')" data-testid="env-facts">
    <NText depth="3" class="intro">{{ t('envFacts.intro') }}</NText>
    <NDescriptions :column="2" bordered size="small" label-placement="left">
      <NDescriptionsItem v-for="row in rows" :key="row.key" :label="row.label">
        <span class="value" :data-testid="`env-${row.key}`">{{ row.text }}</span>
        <NText v-if="row.variable" depth="3" class="var">{{ row.variable }}</NText>
      </NDescriptionsItem>
    </NDescriptions>
  </NCard>
</template>

<style scoped>
.intro {
  display: block;
  margin-bottom: 12px;
  font-size: 12px;
}

.value {
  word-break: break-all;
}

.var {
  display: block;
  font-size: 11px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
</style>
