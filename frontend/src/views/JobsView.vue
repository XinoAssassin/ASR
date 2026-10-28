<script setup lang="ts">
import { useI18n } from 'vue-i18n'

import PlaceholderView from '@/components/PlaceholderView.vue'

const { t } = useI18n()
</script>

<template>
  <PlaceholderView :title="t('views.jobs.title')" />
</template>
