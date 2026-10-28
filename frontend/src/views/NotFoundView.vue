<script setup lang="ts">
import { NButton, NResult } from 'naive-ui'
import { useI18n } from 'vue-i18n'
import { RouterLink } from 'vue-router'

const { t } = useI18n()
</script>

<template>
  <NResult status="404" :title="t('views.notFound.title')">
    <template #footer>
      <RouterLink :to="{ name: 'upload' }">
        <NButton>{{ t('views.notFound.back') }}</NButton>
      </RouterLink>
    </template>
  </NResult>
</template>
