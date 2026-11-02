<script setup lang="ts">
/**
 * First-run wizard of the "desktop + backend" edition: hardware → folders → install → done /
 * failed. Mounted once by App (desktop only); the managed backend store drives it. Closing it
 * ("稍后" / "后台安装") never stops a running setup.
 */
import {
  NAlert,
  NButton,
  NCollapse,
  NCollapseItem,
  NForm,
  NFormItem,
  NInput,
  NModal,
  NPopconfirm,
  NProgress,
  NResult,
  NSpace,
  NSpin,
  NStep,
  NSteps,
  NTag,
  NText,
  useMessage,
} from 'naive-ui'
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'

import type { HardwareSuggestion, ManagedFolder } from '@/platform'
import ProxySettingsForm from '@/components/proxy/ProxySettingsForm.vue'
import { useBackendSwitch } from '@/composables/useBackendSwitch'
import { useHardwareStore } from '@/stores/hardware'
import { folderError, useManagedBackendStore } from '@/stores/managedBackend'
import { LOCAL_PROFILE_ID, useProfilesStore } from '@/stores/profiles'
import { useProxyStore } from '@/stores/proxy'
import { useSettingsStore } from '@/stores/settings'
import { useSidecarStore } from '@/stores/sidecar'
import { formatBytes, formatPercent } from '@/utils/format'

const { t } = useI18n()
const message = useMessage()
const router = useRouter()
const managed = useManagedBackendStore()
const sidecar = useSidecarStore()
const profiles = useProfilesStore()
const backend = useBackendSwitch()
const settings = useSettingsStore()
const hardware = useHardwareStore()

const proxy = useProxyStore()
/** The "网络" step exists when the core supports the proxy setting. */
const withNetwork = computed(() => proxy.ready)
const STEPS = computed(() =>
  withNetwork.value
    ? (['hardware', 'folders', 'network', 'install', 'done'] as const)
    : (['hardware', 'folders', 'install', 'done'] as const),
)
const stepIndex = computed(() => {
  const s = managed.step === 'failed' ? 'install' : managed.step
  return (STEPS.value as readonly string[]).indexOf(s) + 1
})
watch(
  () => managed.wizardOpen,
  (open) => {
    if (open) void proxy.load()
  },
  { immediate: true },
)
const proxyForm = ref<InstanceType<typeof ProxySettingsForm> | null>(null)

function pick(s: HardwareSuggestion): void {
  if (s.available) managed.choice.hardware = s.id
}

const dataError = computed(() => folderError(managed.choice.dataDir))
const modelsError = computed(() => folderError(managed.choice.modelsDir))

async function onInstall(): Promise<void> {
  // The proxy is saved before the setup starts (it downloads through it).
  if (managed.step === 'network' && proxyForm.value) {
    if (!proxyForm.value.valid) return
    try {
      await proxyForm.value.save()
    } catch (e) {
      message.error(t('common.labelDetail', { label: t('managed.network.saveFailed'), detail: (e as Error).message }))
      return
    }
  }
  try {
    await managed.install()
  } catch (e) {
    message.error(t('common.labelDetail', { label: t('managed.install.startFailed'), detail: (e as Error).message }))
  }
}

async function onRetry(): Promise<void> {
  try {
    await managed.retry()
  } catch (e) {
    message.error(t('common.labelDetail', { label: t('managed.install.startFailed'), detail: (e as Error).message }))
  }
}

/** "CPU" / "AMD ROCm": the environment a cancelled / failed switch keeps. */
const keptHardware = computed(() =>
  managed.status.state === 'installed' && managed.status.hardware
    ? t(`managed.hardwareName.${managed.status.hardware}`)
    : null,
)
const cancelledText = computed(() =>
  keptHardware.value
    ? t('managed.install.cancelledSwitch', { hardware: keptHardware.value })
    : t('managed.install.cancelled'),
)

/** A working environment exists (switch / repair): "关闭" instead of "稍后". */
const laterText = computed(() =>
  managed.status.state === 'installed' ? t('managed.wizard.close') : t('managed.wizard.later'),
)

async function onCancel(): Promise<void> {
  try {
    await managed.cancel()
    if (managed.cancelled) message.info(cancelledText.value)
  } catch (e) {
    message.error(t('common.labelDetail', { label: t('managed.install.cancelFailed'), detail: (e as Error).message }))
  }
}

async function onDownloadModels(): Promise<void> {
  managed.closeWizard()
  // The backend just came up: what Settings loaded before is stale.
  void settings.load()
  void hardware.load()
  void settings.loadLocalModels()
  await router.push({ name: 'settings', hash: '#local-models' })
  await nextTick()
  document.getElementById('local-models')?.scrollIntoView({ block: 'start' })
}

// -- folders ----------------------------------------------------------------------------------

/** Placeholder of a folder field: the real default path when the core reports it. */
function defaultText(path: string | null): string {
  return path ? t('managed.folders.defaultAt', { path }) : t('managed.folders.default')
}

async function onBrowse(which: 'data' | 'models'): Promise<void> {
  try {
    await managed.pickFolder(which, t(which === 'data' ? 'managed.folders.pickData' : 'managed.folders.pickModels'))
  } catch (e) {
    message.error(t('common.labelDetail', { label: t('managed.folders.pickFailed'), detail: (e as Error).message }))
  }
}

async function onReveal(which: ManagedFolder): Promise<void> {
  try {
    await managed.reveal(which)
  } catch (e) {
    message.error(t('common.labelDetail', { label: t('managed.card.revealFailed'), detail: (e as Error).message }))
  }
}

// -- progress + log ----------------------------------------------------------------------------

const phaseText = computed(() => {
  const p = managed.progress
  return p ? t(`managed.install.phase.${p.phase}`) : t('managed.install.waiting')
})

const logBox = ref<HTMLElement | null>(null)
/** The log opens by itself when the setup failed. */
const logExpanded = ref<string[]>([])
watch(
  () => managed.step,
  (s) => {
    if (s === 'failed') logExpanded.value = ['log']
    else if (s !== 'install') logExpanded.value = []
  },
  { immediate: true },
)
watch(
  () => managed.log.length && managed.log[managed.log.length - 1],
  () =>
    void nextTick(() => {
      const el = logBox.value
      if (el) el.scrollTop = el.scrollHeight
    }),
)

const failure = computed(
  () =>
    managed.status.error ??
    (managed.progress?.phase === 'failed' ? managed.progress.message : null) ??
    t('managed.failed.unknownError'),
)

// -- done: the 本机 profile follows the sidecar ------------------------------------------------

const sidecarUrl = computed(() => (sidecar.status.state === 'running' ? sidecar.status.url : null))
const followsSidecar = computed(
  () => profiles.isLocalActive && !profiles.find(LOCAL_PROFILE_ID)?.url && !!sidecarUrl.value,
)

async function onUseBackend(): Promise<void> {
  if (await backend.useSidecar()) message.success(t('recorder.sidecar.used', { url: sidecarUrl.value ?? '' }))
}

// Finished while the wizard was closed: say so.
watch(
  () => managed.step,
  (now, before) => {
    if (managed.wizardOpen || before !== 'install') return
    if (now === 'done') message.success(t('managed.done.notify'))
    else if (now === 'failed') message.error(t('managed.failed.notify'))
  },
)

const sizeText = (s: HardwareSuggestion): string | null =>
  s.downloadBytes != null ? t('managed.downloadSize', { size: formatBytes(s.downloadBytes) }) : null
</script>

<template>
  <NModal
    :show="managed.wizardOpen"
    preset="card"
    :title="t('managed.wizard.title')"
    :mask-closable="false"
    :close-on-esc="false"
    :closable="false"
    class="managed-wizard"
    style="width: min(760px, calc(100vw - 32px))"
    data-testid="managed-wizard"
    :data-step="managed.step"
  >
    <NSteps :current="stepIndex" :status="managed.step === 'failed' ? 'error' : 'process'" size="small" class="steps">
      <NStep v-for="s in STEPS" :key="s" :title="t(`managed.wizard.steps.${s}`)" />
    </NSteps>

    <!-- 1. hardware -->
    <template v-if="managed.step === 'hardware'">
      <NText depth="3" class="block">{{ t('managed.wizard.intro') }}</NText>
      <NAlert v-if="managed.cancelled" type="info" class="block" data-testid="managed-cancelled">
        {{ cancelledText }}
      </NAlert>
      <NAlert v-if="managed.detectError" type="warning" class="block" data-testid="managed-detect-error">
        {{ t('managed.hardware.detectFailed', { error: managed.detectError }) }}
        <NButton text type="primary" @click="managed.detect()">{{ t('managed.hardware.redetect') }}</NButton>
      </NAlert>
      <NText strong class="block">{{ t('managed.hardware.title') }}</NText>
      <NSpin :show="managed.detecting && !managed.suggestions.length">
        <div class="hw-list" role="radiogroup" data-testid="managed-hardware-list">
          <div
            v-for="s in managed.suggestions"
            :key="s.id"
            class="hw-card"
            role="radio"
            :tabindex="s.available ? 0 : -1"
            :aria-checked="managed.choice.hardware === s.id"
            :aria-disabled="!s.available"
            :class="{ 'hw-card--selected': managed.choice.hardware === s.id, 'hw-card--disabled': !s.available }"
            :data-testid="`managed-hw-${s.id}`"
            :data-selected="managed.choice.hardware === s.id"
            :data-available="s.available"
            @click="pick(s)"
            @keydown.enter.space.prevent="pick(s)"
          >
            <div class="hw-card__head">
              <span class="hw-card__name">{{ t(`managed.hardwareName.${s.id}`) }}</span>
              <NTag v-if="s.recommended && s.available" type="success" size="small" round>
                {{ t('managed.hardware.recommended') }}
              </NTag>
              <NTag v-if="managed.status.state === 'installed' && managed.status.hardware === s.id" size="small" round>
                {{ t('managed.hardware.current') }}
              </NTag>
              <NTag v-if="!s.available" size="small" round>{{ t('managed.hardware.unavailable') }}</NTag>
            </div>
            <div class="hw-card__label">{{ s.label }}</div>
            <div v-if="s.detail" class="hw-card__detail">{{ s.detail }}</div>
            <div v-if="s.available && sizeText(s)" class="hw-card__size" :data-testid="`managed-hw-size-${s.id}`">
              {{ sizeText(s) }}
            </div>
          </div>
        </div>
      </NSpin>
      <NText depth="3" class="hint">{{ t('managed.hardware.hint') }}</NText>
    </template>

    <!-- 2. folders -->
    <template v-else-if="managed.step === 'folders'">
      <NText strong class="block">{{ t('managed.folders.title') }}</NText>
      <NAlert v-if="managed.status.portable" type="warning" class="block" data-testid="managed-portable-note">
        {{ t('managed.folders.portable') }}
      </NAlert>
      <NForm label-placement="top">
        <NFormItem
          :label="t('managed.folders.data')"
          :validation-status="dataError ? 'error' : undefined"
          :feedback="dataError ? t(`managed.folders.errors.${dataError}`) : t('managed.folders.dataHint')"
        >
          <div class="folder-row">
            <NInput
              v-model:value="managed.choice.dataDir"
              :placeholder="defaultText(managed.status.defaultDataDir)"
              clearable
              data-testid="managed-data-dir"
            />
            <NButton data-testid="managed-browse-data" @click="onBrowse('data')">{{ t('managed.folders.browse') }}</NButton>
          </div>
        </NFormItem>
        <NFormItem
          :label="t('managed.folders.models')"
          :validation-status="modelsError ? 'error' : undefined"
          :feedback="modelsError ? t(`managed.folders.errors.${modelsError}`) : t('managed.folders.modelsHint')"
        >
          <div class="folder-row">
            <NInput
              v-model:value="managed.choice.modelsDir"
              :placeholder="defaultText(managed.status.defaultModelsDir)"
              clearable
              data-testid="managed-models-dir"
            />
            <NButton data-testid="managed-browse-models" @click="onBrowse('models')">
              {{ t('managed.folders.browse') }}
            </NButton>
          </div>
        </NFormItem>
      </NForm>
      <NText depth="3" class="hint">{{ t('managed.folders.keepNote') }}</NText>
    </template>

    <!-- 3. network (proxy) -->
    <template v-else-if="managed.step === 'network'">
      <NText strong class="block">{{ t('managed.network.title') }}</NText>
      <NText depth="3" class="block">{{ t('managed.network.hint') }}</NText>
      <ProxySettingsForm ref="proxyForm" />
    </template>

    <!-- 3. install -->
    <template v-else-if="managed.step === 'install'">
      <NText strong class="block" data-testid="managed-phase" :data-phase="managed.progress?.phase ?? ''">
        {{ phaseText }}
      </NText>
      <NProgress
        type="line"
        :percentage="managed.percent ?? 0"
        :processing="managed.installing"
        :show-indicator="managed.percent != null"
        data-testid="managed-progress"
        :data-percent="managed.percent ?? ''"
      >
        {{ formatPercent(managed.percent) }}
      </NProgress>
      <NText class="status-line" data-testid="managed-message">{{ managed.progress?.message ?? '' }}</NText>
      <NText depth="3" class="hint block">{{ t('managed.install.background') }}</NText>
    </template>

    <!-- done -->
    <template v-else-if="managed.step === 'done'">
      <NResult status="success" :title="t('managed.done.title')" :description="t('managed.done.content')" size="small">
        <template #footer>
          <NSpace vertical align="center">
            <NText v-if="sidecarUrl" data-testid="managed-sidecar-running">
              {{ t('managed.done.sidecarRunning', { url: sidecarUrl }) }}
            </NText>
            <NText v-else depth="3">{{ t('managed.done.sidecarStarting') }}</NText>
            <NSpace justify="center">
              <NButton v-if="sidecarUrl && !followsSidecar" secondary data-testid="managed-use-backend" @click="onUseBackend">
                {{ t('managed.done.useBackend') }}
              </NButton>
              <NButton secondary data-testid="managed-done-open-models" @click="onReveal('models')">
                {{ t('managed.card.openModels') }}
              </NButton>
            </NSpace>
          </NSpace>
        </template>
      </NResult>
    </template>

    <!-- failed -->
    <template v-else>
      <NResult status="error" :title="t('managed.failed.title')" size="small">
        <template #footer>
          <NAlert type="error" :show-icon="false" data-testid="managed-error">{{ failure }}</NAlert>
          <NAlert v-if="keptHardware" type="info" :show-icon="false" class="kept" data-testid="managed-kept">
            {{ t('managed.failed.kept', { hardware: keptHardware }) }}
          </NAlert>
        </template>
      </NResult>
    </template>

    <NCollapse v-if="managed.step === 'install' || managed.step === 'failed'" class="log" v-model:expanded-names="logExpanded">
      <NCollapseItem :title="`${t('managed.install.log')} (${managed.log.length})`" name="log" data-testid="managed-log-toggle">
        <pre ref="logBox" class="log-box" data-testid="managed-log">{{ managed.log.length ? managed.log.join('\n') : t('managed.install.logEmpty') }}</pre>
      </NCollapseItem>
    </NCollapse>

    <template #footer>
      <NSpace justify="end">
        <template v-if="managed.step === 'hardware'">
          <NButton data-testid="managed-later" @click="managed.closeWizard()">{{ laterText }}</NButton>
          <NButton type="primary" :disabled="!managed.choice.hardware" data-testid="managed-next" @click="managed.next()">
            {{ t('managed.wizard.next') }}
          </NButton>
        </template>
        <template v-else-if="managed.step === 'folders'">
          <NButton data-testid="managed-later" @click="managed.closeWizard()">{{ laterText }}</NButton>
          <NButton data-testid="managed-back" @click="managed.back()">{{ t('managed.wizard.back') }}</NButton>
          <NButton
            v-if="withNetwork"
            type="primary"
            :disabled="!managed.foldersValid"
            data-testid="managed-next"
            @click="managed.next(true)"
          >
            {{ t('managed.wizard.next') }}
          </NButton>
          <NButton
            v-else
            type="primary"
            :disabled="!managed.foldersValid"
            :loading="managed.requesting"
            data-testid="managed-install"
            @click="onInstall"
          >
            {{ t('managed.wizard.start') }}
          </NButton>
        </template>
        <template v-else-if="managed.step === 'network'">
          <NButton data-testid="managed-later" @click="managed.closeWizard()">{{ laterText }}</NButton>
          <NButton data-testid="managed-back" @click="managed.back()">{{ t('managed.wizard.back') }}</NButton>
          <NButton
            type="primary"
            :disabled="proxyForm ? !proxyForm.valid : false"
            :loading="managed.requesting || proxy.saving"
            data-testid="managed-install"
            @click="onInstall"
          >
            {{ t('managed.wizard.start') }}
          </NButton>
        </template>
        <template v-else-if="managed.step === 'install'">
          <NPopconfirm
            :positive-button-props="{ type: 'error', 'data-testid': 'managed-cancel-confirm' } as never"
            @positive-click="onCancel"
          >
            <template #trigger>
              <NButton :disabled="!managed.installing" :loading="managed.requesting" data-testid="managed-cancel">
                {{ t('managed.install.cancel') }}
              </NButton>
            </template>
            {{ t('managed.install.cancelConfirm') }}
          </NPopconfirm>
          <NButton data-testid="managed-hide" @click="managed.closeWizard()">{{ t('managed.wizard.hide') }}</NButton>
        </template>
        <template v-else-if="managed.step === 'done'">
          <NButton data-testid="managed-finish" @click="managed.closeWizard()">{{ t('managed.done.finish') }}</NButton>
          <NButton type="primary" data-testid="managed-download-models" @click="onDownloadModels">
            {{ t('managed.done.downloadModels') }}
          </NButton>
        </template>
        <template v-else>
          <NButton data-testid="managed-later" @click="managed.closeWizard()">{{ laterText }}</NButton>
          <NButton data-testid="managed-other-hardware" @click="managed.startOver()">
            {{ t('managed.failed.otherHardware') }}
          </NButton>
          <NButton type="primary" :loading="managed.requesting" data-testid="managed-retry" @click="onRetry">
            {{ t('managed.failed.retry') }}
          </NButton>
        </template>
      </NSpace>
    </template>
  </NModal>
</template>

<style scoped>
.steps {
  margin-bottom: 20px;
}

.block {
  display: block;
  margin-bottom: 12px;
}

.hint {
  font-size: 12px;
}

.hw-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 12px;
  margin-bottom: 12px;
}

.hw-card {
  border: 1px solid var(--n-border-color, rgba(128, 128, 128, 0.3));
  border-radius: 6px;
  padding: 12px;
  cursor: pointer;
  transition: border-color 0.15s;
  min-width: 0;
}

.hw-card:hover:not(.hw-card--disabled),
.hw-card:focus-visible {
  border-color: #36ad6a;
  outline: none;
}

.hw-card--selected {
  border-color: #18a058;
  box-shadow: 0 0 0 1px #18a058;
}

.hw-card--disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.hw-card__head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 4px;
}

.hw-card__name {
  font-weight: 600;
}

.hw-card__label {
  font-size: 13px;
  overflow-wrap: anywhere;
}

.hw-card__detail,
.hw-card__size {
  font-size: 12px;
  opacity: 0.7;
  margin-top: 4px;
  overflow-wrap: anywhere;
}

.folder-row {
  display: flex;
  gap: 8px;
  width: 100%;
}

.kept {
  margin-top: 8px;
}

.status-line {
  display: block;
  margin: 8px 0 4px;
}

.log {
  margin-top: 12px;
}

.log-box {
  max-height: 220px;
  overflow: auto;
  margin: 0;
  padding: 8px;
  font-size: 12px;
  line-height: 1.4;
  white-space: pre-wrap;
  word-break: break-all;
  background: rgba(128, 128, 128, 0.08);
  border-radius: 4px;
}
</style>
