<script setup lang="ts">
/**
 * Settings → 本地后端, "desktop + backend" edition: what the managed backend was installed for
 * (hardware, folders, environment size, state) and the actions that re-run the setup. Rendered
 * inside the sidecar card, which then hides its manual command / args fields.
 */
import {
  NAlert,
  NButton,
  NDescriptions,
  NDescriptionsItem,
  NPopconfirm,
  NTag,
  NText,
  useMessage,
} from 'naive-ui'
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

import type { ManagedState } from '@/platform'
import { useManagedBackendStore } from '@/stores/managedBackend'
import { formatBytes } from '@/utils/format'

const { t } = useI18n()
const message = useMessage()
const managed = useManagedBackendStore()

const st = computed(() => managed.status)

const STATE_TYPE: Record<ManagedState, 'default' | 'info' | 'success' | 'error' | 'warning'> = {
  not_installed: 'warning',
  installing: 'info',
  installed: 'success',
  failed: 'error',
}

const stateText = computed(() => {
  const pct = st.value.state === 'installing' && managed.percent != null ? ` ${Math.round(managed.percent)}%` : ''
  return t(`managed.state.${st.value.state}`) + pct
})

async function onRepair(): Promise<void> {
  try {
    await managed.repair()
  } catch (e) {
    message.error(t('common.labelDetail', { label: t('managed.card.repairFailed'), detail: (e as Error).message }))
  }
}
</script>

<template>
  <div class="managed-panel" data-testid="managed-panel" :data-state="st.state">
    <NText depth="3" class="intro">{{ t('managed.card.intro') }}</NText>
    <NDescriptions :column="2" bordered size="small" label-placement="left" class="block">
      <NDescriptionsItem :label="t('managed.card.edition')">{{ t(`managed.edition.${st.edition}`) }}</NDescriptionsItem>
      <NDescriptionsItem :label="t('managed.card.state')">
        <NTag :type="STATE_TYPE[st.state]" size="small" data-testid="managed-state" :data-state="st.state">
          {{ stateText }}
        </NTag>
      </NDescriptionsItem>
      <NDescriptionsItem :label="t('managed.card.hardware')">
        <span data-testid="managed-hardware">{{ st.hardware ? t(`managed.hardwareName.${st.hardware}`) : '—' }}</span>
      </NDescriptionsItem>
      <NDescriptionsItem :label="t('managed.card.version')">{{ st.backendVersion ?? '—' }}</NDescriptionsItem>
      <NDescriptionsItem :label="t('managed.card.dataDir')" :span="2">
        <NText code class="path" data-testid="managed-data-dir-value">{{ st.dataDir ?? t('managed.card.defaultDir') }}</NText>
      </NDescriptionsItem>
      <NDescriptionsItem :label="t('managed.card.modelsDir')" :span="2">
        <NText code class="path" data-testid="managed-models-dir-value">{{ st.modelsDir ?? t('managed.card.defaultDir') }}</NText>
      </NDescriptionsItem>
      <NDescriptionsItem :label="t('managed.card.envSize')" :span="2">
        <span data-testid="managed-env-size">{{ st.envSizeBytes != null ? formatBytes(st.envSizeBytes) : '—' }}</span>
      </NDescriptionsItem>
    </NDescriptions>

    <NAlert v-if="st.state === 'failed' && st.error" type="error" class="block" data-testid="managed-card-error">
      {{ st.error }}
    </NAlert>

    <div class="actions block">
      <NButton
        v-if="st.state === 'not_installed'"
        type="primary"
        data-testid="managed-card-install"
        @click="managed.openWizard()"
      >
        {{ t('managed.card.install') }}
      </NButton>
      <NButton v-else-if="st.state === 'installing'" data-testid="managed-card-progress" @click="managed.openWizard()">
        {{ t('managed.card.showProgress') }}
      </NButton>
      <NButton v-else-if="st.state === 'failed'" type="primary" data-testid="managed-card-details" @click="managed.openWizard()">
        {{ t('managed.card.showError') }}
      </NButton>
      <template v-if="st.state === 'installed' || st.state === 'failed'">
        <NButton data-testid="managed-switch-hardware" @click="managed.switchHardware()">
          {{ t('managed.card.switchHardware') }}
        </NButton>
      </template>
      <NPopconfirm
        v-if="st.state === 'installed'"
        :positive-button-props="{ type: 'warning', 'data-testid': 'managed-repair-confirm' } as never"
        @positive-click="onRepair"
      >
        <template #trigger>
          <NButton :loading="managed.requesting" data-testid="managed-repair">{{ t('managed.card.repair') }}</NButton>
        </template>
        {{ t('managed.card.repairConfirm') }}
      </NPopconfirm>
    </div>
  </div>
</template>

<style scoped>
.intro {
  display: block;
  margin-bottom: 12px;
}

.block {
  margin-bottom: 16px;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 12px;
}

.path {
  overflow-wrap: anywhere;
}
</style>
