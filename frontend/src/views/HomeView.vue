<script setup lang="ts">
import { NButton, NCard, NDescriptions, NDescriptionsItem, NResult, NSpin } from 'naive-ui'
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

import { getBackendUrl } from '@/api/client'
import { useHealthStore } from '@/stores/health'

const { t } = useI18n()
const health = useHealthStore()
const backendUrl = computed(() => getBackendUrl() || t('health.sameOrigin'))
</script>

<template>
  <NCard :title="t('health.title')">
    <NSpin :show="health.state === 'checking'">
      <NDescriptions v-if="health.state === 'connected' && health.info" :column="1" bordered>
        <NDescriptionsItem :label="t('health.backendUrl')">{{ backendUrl }}</NDescriptionsItem>
        <NDescriptionsItem :label="t('health.version')">{{ health.info.version }}</NDescriptionsItem>
        <NDescriptionsItem :label="t('health.decoder')">{{ health.info.decoder }}</NDescriptionsItem>
        <NDescriptionsItem :label="t('health.ffmpeg')">
          {{ health.info.ffmpeg_version ?? '—' }}
        </NDescriptionsItem>
        <NDescriptionsItem :label="t('health.apiKeyRequired')">
          {{ health.info.api_key_required ? t('health.yes') : t('health.no') }}
        </NDescriptionsItem>
      </NDescriptions>
      <NResult
        v-else-if="health.state === 'error'"
        status="error"
        :title="t('health.error')"
        :description="`${backendUrl} — ${health.error ?? ''}`"
      >
        <template #footer>
          <NButton @click="health.check()">{{ t('health.retry') }}</NButton>
        </template>
      </NResult>
      <div v-else class="checking">{{ t('health.checking') }}</div>
    </NSpin>
  </NCard>
</template>

<style scoped>
.checking {
  min-height: 80px;
}
</style>
