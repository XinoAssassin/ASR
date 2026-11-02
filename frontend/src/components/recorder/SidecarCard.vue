<script setup lang="ts">
/**
 * Settings → "本地后端" (desktop only, M10): the optional `asr serve` process the desktop app
 * starts. Config, live status (`sidecar://status`), start / stop, and "use this backend".
 * In the "desktop + backend" edition the app manages the command / args / port itself: the
 * manual fields are replaced by the managed backend panel (hardware, folders, reinstall).
 */
import {
  NAlert,
  NButton,
  NCard,
  NDynamicInput,
  NForm,
  NFormItem,
  NInput,
  NInputNumber,
  NSpace,
  NSwitch,
  NTag,
  NText,
  useMessage,
} from 'naive-ui'
import { computed, defineAsyncComponent, onMounted, reactive, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import type { SidecarConfig, SidecarState } from '@/platform'
import { useBackendSwitch } from '@/composables/useBackendSwitch'
import { useManagedBackendStore } from '@/stores/managedBackend'
import { useProfilesStore } from '@/stores/profiles'
import { defaultSidecarConfig, syncPortArg, useSidecarStore } from '@/stores/sidecar'

const { t } = useI18n()
const message = useMessage()
const sidecar = useSidecarStore()
const profiles = useProfilesStore()
const backend = useBackendSwitch()
const managed = useManagedBackendStore()
// Bundled edition only: loaded on demand.
const ManagedBackendPanel = defineAsyncComponent(() => import('@/components/managed/ManagedBackendPanel.vue'))
/** Bundled edition: no manual command; external (or an older core): the full form. */
const managedMode = computed(() => managed.bundled)
const showForm = computed(() => managed.loaded && !managed.bundled)
/** Nothing to start before the managed setup finished. */
const canStart = computed(() => !managedMode.value || managed.installed)

const form = reactive<SidecarConfig & { cwdText: string }>({ ...defaultSidecarConfig(), cwdText: '' })

function fill(): void {
  const c = sidecar.config
  if (!c) return
  Object.assign(form, { ...c, args: [...c.args], cwdText: c.cwd ?? '' })
}
watch(() => sidecar.config, fill, { immediate: true })

function onPort(port: number | null): void {
  if (port == null) return
  form.port = port
  form.args = syncPortArg(form.args, port)
}

function current(): SidecarConfig {
  return {
    enabled: form.enabled,
    command: form.command,
    args: [...form.args],
    cwd: form.cwdText.trim() || null,
    port: form.port,
  }
}

const STATE_TYPE: Record<SidecarState, 'default' | 'info' | 'success' | 'error'> = {
  stopped: 'default',
  starting: 'info',
  running: 'success',
  failed: 'error',
}

const st = computed(() => sidecar.status)
const busy = computed(() => sidecar.starting || sidecar.stopping || st.value.state === 'starting')
const inUse = computed(
  () => !!st.value.url && profiles.isLocalActive && profiles.activeUrl === st.value.url,
)

async function save(): Promise<boolean> {
  try {
    await sidecar.saveConfig(current())
    return true
  } catch (e) {
    message.error(t('common.labelDetail', { label: t('recorder.sidecar.saveFailed'), detail: (e as Error).message }))
    return false
  }
}

async function onSave(): Promise<void> {
  if (await save()) message.success(t('settings.saved'))
}

async function onStart(): Promise<void> {
  // Managed: the core wrote the config itself; nothing to save.
  if (!managedMode.value) {
    if (!form.command.trim()) {
      message.warning(t('recorder.sidecar.needCommand'))
      return
    }
    // The core starts what it has stored: save the form first.
    if (!(await save())) return
  }
  try {
    const result = await sidecar.start()
    if (result.state === 'running') message.success(t('recorder.sidecar.started'))
  } catch (e) {
    message.error(t('common.labelDetail', { label: t('recorder.sidecar.startFailed'), detail: (e as Error).message }))
  }
}

async function onStop(): Promise<void> {
  try {
    await sidecar.stop()
  } catch (e) {
    message.error(t('common.labelDetail', { label: t('recorder.sidecar.stopFailed'), detail: (e as Error).message }))
  }
}

async function onUse(): Promise<void> {
  try {
    const url = sidecar.status.url
    if (sidecar.status.state !== 'running' || !url) throw new Error('sidecar is not running')
    if (await backend.useSidecar()) message.success(t('recorder.sidecar.used', { url }))
  } catch (e) {
    message.error(t('common.labelDetail', { label: t('recorder.sidecar.useFailed'), detail: (e as Error).message }))
  }
}

onMounted(() => {
  void sidecar.init()
  void managed.init()
})
</script>

<template>
  <NCard id="sidecar" :title="t('recorder.sidecar.title')" data-testid="sidecar-settings">
    <template #header-extra>
      <NTag :type="STATE_TYPE[st.state]" round data-testid="sidecar-state" :data-state="st.state">
        {{ t(`recorder.sidecar.state.${st.state}`) }}
        <template v-if="st.pid != null"> · PID {{ st.pid }}</template>
      </NTag>
    </template>

    <NAlert v-if="sidecar.error" type="warning" :show-icon="false" data-testid="sidecar-unavailable">
      {{ t('recorder.sidecar.unavailable', { error: sidecar.error }) }}
    </NAlert>
    <template v-else>
      <ManagedBackendPanel v-if="managedMode" />
      <NText v-else depth="3" class="intro">{{ t('recorder.sidecar.intro') }}</NText>
      <NAlert v-if="st.error" type="error" class="block" data-testid="sidecar-error">{{ st.error }}</NAlert>

      <NForm v-if="showForm" label-placement="left" label-width="120" :disabled="!sidecar.config">
        <NFormItem :label="t('recorder.sidecar.enabled')">
          <NSpace align="center">
            <NSwitch v-model:value="form.enabled" data-testid="sidecar-enabled" />
            <NText depth="3">{{ t('recorder.sidecar.enabledHint') }}</NText>
          </NSpace>
        </NFormItem>
        <NFormItem :label="t('recorder.sidecar.command')">
          <NInput
            v-model:value="form.command"
            :placeholder="t('recorder.sidecar.commandPlaceholder')"
            data-testid="sidecar-command"
          />
        </NFormItem>
        <NFormItem :label="t('recorder.sidecar.args')">
          <NDynamicInput
            v-model:value="form.args"
            :placeholder="t('recorder.sidecar.argPlaceholder')"
            data-testid="sidecar-args"
          />
        </NFormItem>
        <NFormItem :label="t('recorder.sidecar.cwd')">
          <NInput
            v-model:value="form.cwdText"
            :placeholder="t('recorder.sidecar.cwdPlaceholder')"
            clearable
            data-testid="sidecar-cwd"
          />
        </NFormItem>
        <NFormItem :label="t('recorder.sidecar.port')">
          <NSpace vertical size="small">
            <NInputNumber
              :value="form.port"
              :min="1"
              :max="65535"
              :show-button="false"
              style="width: 140px"
              data-testid="sidecar-port"
              @update:value="onPort"
            />
            <NText depth="3" class="hint">{{ t('recorder.sidecar.portHint', { port: form.port }) }}</NText>
          </NSpace>
        </NFormItem>
      </NForm>

      <!-- A plain flex row: NSpace re-keys the v-if children (duplicate key warnings). -->
      <div class="actions">
        <NButton
          v-if="showForm"
          :loading="sidecar.saving"
          :disabled="!sidecar.config"
          data-testid="sidecar-save"
          @click="onSave"
        >
          {{ t('recorder.sidecar.save') }}
        </NButton>
        <NButton
          v-if="st.state !== 'running' && canStart"
          type="primary"
          :loading="sidecar.starting || st.state === 'starting'"
          :disabled="!sidecar.config || busy"
          data-testid="sidecar-start"
          @click="onStart"
        >
          {{ t('recorder.sidecar.start') }}
        </NButton>
        <NButton
          v-else-if="st.state === 'running'"
          :loading="sidecar.stopping"
          :disabled="busy"
          data-testid="sidecar-stop"
          @click="onStop"
        >
          {{ t('recorder.sidecar.stop') }}
        </NButton>
        <template v-if="st.state === 'running' && st.url">
          <NText code data-testid="sidecar-url">{{ st.url }}</NText>
          <NTag v-if="inUse" type="success" size="small" data-testid="sidecar-in-use">
            {{ t('recorder.sidecar.usingBackend') }}
          </NTag>
          <NButton v-else type="primary" secondary data-testid="sidecar-use" @click="onUse">
            {{ t('recorder.sidecar.useBackend') }}
          </NButton>
        </template>
      </div>
    </template>
  </NCard>
</template>

<style scoped>
.intro {
  display: block;
  margin-bottom: 12px;
}

.block {
  margin-bottom: 16px;
}

.hint {
  font-size: 12px;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
}
</style>
