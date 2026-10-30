<script setup lang="ts">
/** Small "env" marker for a setting that only the server environment sets (`env_only`). */
import { NTag } from 'naive-ui'
import { useI18n } from 'vue-i18n'

defineProps<{ name: string }>()

const { t } = useI18n()
</script>

<template>
  <NTag
    size="tiny"
    :bordered="false"
    class="env-tag"
    :title="t('envFacts.fromEnvTitle', { name })"
    :data-env="name"
    data-testid="env-tag"
  >
    {{ t('envFacts.fromEnv') }} · {{ name }}
  </NTag>
</template>

<style scoped>
.env-tag {
  margin-left: 6px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  vertical-align: middle;
}
</style>
