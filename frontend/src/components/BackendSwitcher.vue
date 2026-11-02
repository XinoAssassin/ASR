<script setup lang="ts">
/**
 * Top bar: the active backend profile (with its connection state and the hardware local jobs
 * run on) and a menu to switch profile, start the desktop sidecar, or manage the profiles.
 */
import { type DropdownOption, NButton, NDropdown, NTag, NTooltip, useMessage } from 'naive-ui'
import { computed, h, watch, type HTMLAttributes } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'

import { useBackendSwitch } from '@/composables/useBackendSwitch'
import { useHardwareStore } from '@/stores/hardware'
import { useHealthStore } from '@/stores/health'
import { useManagedBackendStore } from '@/stores/managedBackend'
import { LOCAL_PROFILE_ID, useProfilesStore } from '@/stores/profiles'
import { useSettingsStore } from '@/stores/settings'
import { useSidecarStore } from '@/stores/sidecar'
import { activeHardwareLabel } from '@/utils/hardware'

const { t } = useI18n()
const message = useMessage()
const router = useRouter()
const profiles = useProfilesStore()
const health = useHealthStore()
const hardware = useHardwareStore()
const settings = useSettingsStore()
const sidecar = useSidecarStore()
const managed = useManagedBackendStore()
const { switchTo, profileName } = useBackendSwitch()

/** HTML attributes of a menu entry (Naive's typing has no `data-*`). */
const testId = (id: string): HTMLAttributes => ({ 'data-testid': id }) as HTMLAttributes

const START_LOCAL = '__start-local'
const MANAGE = '__manage'
const MANAGED_SETUP = '__managed-setup'

const sidecarRunning = computed(() => sidecar.status.state === 'running')
const sidecarBusy = computed(() => sidecar.starting || sidecar.status.state === 'starting')

/** Bundled edition, backend not usable yet: "未安装" / "安装中 45%" / "安装失败" on 本机. */
const managedTag = computed(() => {
  if (!managed.needsSetup) return null
  const state = managed.status.state
  const pct = state === 'installing' && managed.percent != null ? ` ${Math.round(managed.percent)}%` : ''
  return {
    state,
    text: t(`managed.state.${state}`) + pct,
    type: state === 'failed' ? ('error' as const) : state === 'installing' ? ('info' as const) : ('warning' as const),
  }
})

function managedTagNode() {
  const tag = managedTag.value
  if (!tag) return null
  return h(
    NTag,
    {
      size: 'tiny',
      type: tag.type,
      round: true,
      style: 'margin-left: 6px',
      'data-testid': 'backend-local-managed',
      'data-state': tag.state,
    } as never,
    () => tag.text,
  )
}

function urlText(id: string): string {
  const p = profiles.find(id)
  return (p && profiles.urlOf(p)) || t('health.sameOrigin')
}

const options = computed<DropdownOption[]>(() => {
  const items: DropdownOption[] = profiles.profiles.map((p) => ({
    key: p.id,
    props: testId(`backend-option-${p.id}`),
    label: () =>
      h('div', { class: 'backend-option' }, [
        h('span', { class: 'backend-option__name' }, [
          profileName(p),
          p.id === profiles.activeId
            ? h(NTag, { size: 'tiny', type: 'success', round: true, style: 'margin-left: 6px' }, () => t('profiles.active'))
            : null,
          p.id === LOCAL_PROFILE_ID ? managedTagNode() : null,
        ]),
        h('span', { class: 'backend-option__url' }, urlText(p.id)),
      ]),
  }))
  items.push({ type: 'divider', key: '__d1' })
  if (managed.needsSetup) {
    const state = managed.status.state === 'not_installed' ? 'install' : managed.status.state
    items.push({
      key: MANAGED_SETUP,
      label: t(`managed.switcher.${state}`),
      props: testId('backend-managed-setup'),
    })
  } else if (sidecar.available && !sidecarRunning.value) {
    items.push({
      key: START_LOCAL,
      label: t('profiles.startLocal'),
      disabled: sidecarBusy.value,
      props: testId('backend-start-local'),
    })
  }
  items.push({ key: MANAGE, label: t('profiles.manage'), props: testId('backend-manage') })
  return items
})

async function startLocal(): Promise<void> {
  if (!sidecar.config?.command.trim()) {
    message.warning(t('profiles.localNeedCommand'))
    void router.push({ name: 'settings', hash: '#sidecar' })
    return
  }
  try {
    const status = await sidecar.start()
    if (status.state === 'running') {
      message.success(t('profiles.localStarted'))
      if (profiles.activeId !== LOCAL_PROFILE_ID) await switchTo(LOCAL_PROFILE_ID)
    } else {
      message.error(t('common.labelDetail', { label: t('profiles.localStartFailed'), detail: status.error ?? status.state }))
    }
  } catch (e) {
    message.error(t('common.labelDetail', { label: t('profiles.localStartFailed'), detail: (e as Error).message }))
  }
}

function onSelect(key: string | number): void {
  const k = String(key)
  if (k === MANAGE) void router.push({ name: 'settings', hash: '#backends' })
  else if (k === START_LOCAL) void startLocal()
  else if (k === MANAGED_SETUP) managed.openWizard()
  else void switchTo(k)
}

const stateType = computed(() => {
  switch (health.state) {
    case 'connected':
      return 'success' as const
    case 'error':
      return 'error' as const
    default:
      return 'default' as const
  }
})

const hardwareText = computed(() =>
  settings.server || hardware.info
    ? activeHardwareLabel(settings.server?.local.device, hardware.info)
    : null,
)

// The top bar is always there: load what it shows for every backend it points at.
watch(
  () => profiles.epoch,
  () => {
    if (!settings.server && !settings.loading) void settings.load()
    if (!hardware.loaded && !hardware.loading) void hardware.load()
  },
  { immediate: true },
)
</script>

<template>
  <div class="backend-switcher">
    <NDropdown trigger="click" :options="options" placement="bottom-end" @select="onSelect">
      <NButton
        size="small"
        secondary
        :type="stateType"
        :loading="profiles.switching"
        data-testid="backend-switcher"
        :data-profile="profiles.activeId"
        :title="t('profiles.switcherTitle', { name: profileName(profiles.active), url: profiles.activeUrl || t('health.sameOrigin') })"
      >
        <span class="backend-switcher__dot" :data-state="health.state" />
        {{ profileName(profiles.active) }}
      </NButton>
    </NDropdown>
    <NTag
      v-if="managedTag && profiles.isLocalActive"
      size="small"
      round
      :type="managedTag.type"
      class="managed-tag"
      data-testid="backend-managed-state"
      :data-state="managedTag.state"
      @click="managed.openWizard()"
    >
      {{ managedTag.text }}
    </NTag>
    <NTooltip v-if="hardwareText">
      <template #trigger>
        <NTag size="small" round :bordered="false" data-testid="active-hardware">{{ hardwareText }}</NTag>
      </template>
      {{ t('hardware.activeTitle') }}
    </NTooltip>
  </div>
</template>

<style scoped>
.backend-switcher {
  display: flex;
  align-items: center;
  gap: 8px;
  white-space: nowrap;
}

.managed-tag {
  cursor: pointer;
}

.backend-switcher__dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  margin-right: 6px;
  border-radius: 50%;
  background: #999;
}

.backend-switcher__dot[data-state='connected'] {
  background: #18a058;
}

.backend-switcher__dot[data-state='error'] {
  background: #d03050;
}
</style>

<style>
.backend-option {
  display: flex;
  flex-direction: column;
  line-height: 1.3;
  padding: 2px 0;
}

.backend-option__url {
  font-size: 12px;
  opacity: 0.6;
}
</style>
