<script setup lang="ts">
/** Collapsible recording tips per source (docs/research/recording-sources.md). */
import { NCollapse, NCollapseItem, NText } from 'naive-ui'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()
const sources = ['iphone', 'watch', 'windows'] as const
</script>

<template>
  <NCollapse data-testid="source-tips">
    <NCollapseItem :title="t('tips.title')" name="tips">
      <div v-for="source in sources" :key="source" class="tip">
        <NText strong>{{ t(`tips.${source}.title`) }}</NText>
        <p>{{ t(`tips.${source}.text`) }}</p>
      </div>
      <NText depth="3">{{ t('tips.preprocess') }}</NText>
    </NCollapseItem>
  </NCollapse>
</template>

<style scoped>
.tip p {
  margin: 4px 0 12px;
  line-height: 1.6;
}
</style>
