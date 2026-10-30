<script setup lang="ts">
/**
 * Where a setting's effective value comes from (默认 / 环境变量 / 配置文件 / 界面设置) plus, for a
 * value set in the UI, a "reset" action that clears the override (the parent sends `""`).
 */
import { NButton, NTag } from 'naive-ui'
import { useI18n } from 'vue-i18n'

import type { SettingSource, SettingSourceKey } from '@/api'

withDefaults(
  defineProps<{
    field: SettingSourceKey
    source: SettingSource | null
    /** false: tag only (the API key fields have their own "清除" button). */
    resettable?: boolean
    /** DashScope LLM endpoint / key that inherits the cloud value: "继承云端识别", not "默认". */
    inherited?: boolean
    busy?: boolean
  }>(),
  { resettable: true, inherited: false, busy: false },
)
defineEmits<{ reset: [] }>()

const { t } = useI18n()

const TYPE = { default: 'default', env: 'info', config: 'warning', settings: 'success' } as const
</script>

<template>
  <span v-if="source" class="source">
    <NTag
      size="tiny"
      :type="inherited ? 'info' : TYPE[source]"
      :bordered="false"
      :title="t(`settings.online.sourceTitles.${inherited ? 'inherited' : source}`)"
      :data-source="inherited ? 'inherited' : source"
      :data-testid="`source-${field}`"
    >
      {{ t(`settings.online.sources.${inherited ? 'inherited' : source}`) }}
    </NTag>
    <NButton
      v-if="resettable && source === 'settings'"
      text
      size="tiny"
      type="primary"
      :disabled="busy"
      :title="t('settings.online.resetTitle')"
      :data-testid="`reset-${field}`"
      @click="$emit('reset')"
    >
      {{ t('settings.online.reset') }}
    </NButton>
  </span>
</template>

<style scoped>
.source {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
}
</style>
