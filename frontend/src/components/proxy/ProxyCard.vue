<script setup lang="ts">
/**
 * Settings → 网络代理 (desktop only, both editions): the app's proxy for the managed setup, the
 * local backend (model downloads, cloud recognition, LLM) and recording uploads. The sidecar only
 * picks a new setting up when it starts, so after saving a running one can be restarted here.
 * Hidden with an older core that has no proxy commands.
 */
import { NAlert, NButton, NCard, NSpace, NText, useMessage } from 'naive-ui'
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import ProxySettingsForm from '@/components/proxy/ProxySettingsForm.vue'
import { useProxyStore } from '@/stores/proxy'
import { useSidecarStore } from '@/stores/sidecar'

const { t } = useI18n()
const message = useMessage()
const proxy = useProxyStore()
const sidecar = useSidecarStore()

const form = ref<InstanceType<typeof ProxySettingsForm> | null>(null)
/** Saved while the sidecar was running: it still uses the old proxy until restarted. */
const needsRestart = ref(false)
const restarting = ref(false)

const sidecarRunning = computed(() => sidecar.status.state === 'running')

async function onSave(): Promise<void> {
  if (!form.value?.valid) return
  try {
    await form.value.save()
    message.success(t('proxy.saved'))
    needsRestart.value = sidecarRunning.value
  } catch (e) {
    message.error(t('common.labelDetail', { label: t('proxy.saveFailed'), detail: (e as Error).message }))
  }
}

async function onRestart(): Promise<void> {
  restarting.value = true
  try {
    await sidecar.stop()
    const status = await sidecar.start()
    if (status.state === 'running') {
      needsRestart.value = false
      message.success(t('proxy.restarted'))
    } else {
      message.error(t('common.labelDetail', { label: t('proxy.restartFailed'), detail: status.error ?? status.state }))
    }
  } catch (e) {
    message.error(t('common.labelDetail', { label: t('proxy.restartFailed'), detail: (e as Error).message }))
  } finally {
    restarting.value = false
  }
}

onMounted(() => {
  void proxy.load()
  void sidecar.init()
})
</script>

<template>
  <NCard v-if="proxy.ready" id="proxy" :title="t('proxy.title')" data-testid="proxy-card">
    <NText depth="3" class="intro">{{ t('proxy.intro') }}</NText>
    <ProxySettingsForm ref="form" />
    <NSpace class="actions">
      <NButton
        type="primary"
        :loading="proxy.saving"
        :disabled="!form?.valid || !form?.dirty"
        data-testid="proxy-save"
        @click="onSave"
      >
        {{ t('proxy.save') }}
      </NButton>
    </NSpace>
    <NAlert v-if="needsRestart && sidecarRunning" type="info" class="restart" data-testid="proxy-restart-hint">
      <div class="restart-row">
        <span>{{ t('proxy.restartHint') }}</span>
        <NButton size="small" type="primary" :loading="restarting" data-testid="proxy-restart" @click="onRestart">
          {{ t('proxy.restart') }}
        </NButton>
      </div>
    </NAlert>
  </NCard>
</template>

<style scoped>
.intro {
  display: block;
  margin-bottom: 12px;
}

.actions {
  margin-top: 16px;
}

.restart {
  margin-top: 16px;
}

.restart-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px 12px;
}
</style>
