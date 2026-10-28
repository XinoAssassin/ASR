<script setup lang="ts">
import { NCard, NEmpty } from 'naive-ui'
import { useI18n } from 'vue-i18n'

defineProps<{ title: string }>()

const { t } = useI18n()
</script>

<template>
  <NCard :title="title">
    <NEmpty :description="t('placeholder.comingSoon')" />
    <slot />
  </NCard>
</template>
