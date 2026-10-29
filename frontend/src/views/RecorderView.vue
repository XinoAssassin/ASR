<script setup lang="ts">
/**
 * Desktop recorder (PLAN §6.3): one-click start/stop, elapsed time, level meters for system audio
 * and microphone, source toggles, device pickers, output folder, warnings and drift, and the
 * recordings with their upload state. In the web build this page only explains that it needs the
 * desktop app; nothing here touches Tauri directly (all IPC goes through the recorder store).
 */
import {
  NAlert,
  NButton,
  NCard,
  NInput,
  NRadioButton,
  NRadioGroup,
  NResult,
  NSelect,
  NSpace,
  NSwitch,
  NTag,
  NText,
} from 'naive-ui'
import { computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { RouterLink } from 'vue-router'

import LevelMeter from '@/components/recorder/LevelMeter.vue'
import RecordingsList from '@/components/recorder/RecordingsList.vue'
import type { Device } from '@/platform'
import { useRecorderStore } from '@/stores/recorder'
import { formatDuration } from '@/utils/format'

const { t } = useI18n()
const recorder = useRecorderStore()

const status = computed(() => recorder.status)
const recording = computed(() => status.value.state === 'recording')
const finalizing = computed(() => status.value.state === 'finalizing')

function deviceOptions(list: Device[]) {
  return [
    { label: t('recorder.defaultDevice'), value: '' },
    ...list.map((d) => ({
      label: d.isDefault ? `${d.name}${t('recorder.defaultMark')}` : d.name,
      value: d.id,
    })),
  ]
}

const outputOptions = computed(() => deviceOptions(recorder.devices.outputs))
const inputOptions = computed(() => deviceOptions(recorder.devices.inputs))

// NSelect cannot hold null as a selectable value; '' stands for "system default".
const outputDevice = computed({
  get: () => recorder.options.outputDevice ?? '',
  set: (v: string) => (recorder.options.outputDevice = v || null),
})
const inputDevice = computed({
  get: () => recorder.options.inputDevice ?? '',
  set: (v: string) => (recorder.options.inputDevice = v || null),
})
const outDir = computed({
  get: () => recorder.options.outDir ?? '',
  set: (v: string) => (recorder.options.outDir = v.trim() ? v : null),
})

const processOptions = computed(() =>
  recorder.processes.map((p) => ({
    label: p.title ? `${p.name} — ${p.title}` : p.name,
    value: p.pid,
  })),
)
const processValue = computed({
  get: () => recorder.processId,
  set: (pid: number | null) => recorder.selectProcess(pid),
})

function onSourceMode(mode: 'device' | 'process'): void {
  recorder.sourceMode = mode
  if (mode === 'process' && !recorder.processes.length) void recorder.refreshProcesses()
}

/** While recording: what the core really captures (it may drop a missing mic). */
const captured = computed(() => (recorder.active ? status.value.sources : null))
const systemOff = computed(() => !!captured.value && !captured.value.system)
const micOff = computed(() => !!captured.value && !captured.value.mic)
const capturingLabel = computed(() => {
  if (!recorder.active || !status.value.sources.system) return null
  const proc = recorder.capturedProcess
  return proc ? t('recorder.capturing', { name: proc.name }) : t('recorder.capturingDevice')
})

const noSource = computed(() => !recorder.options.system && !recorder.options.mic)
const locked = computed(() => recorder.active || recorder.busy)

const elapsed = computed(() => formatDuration(status.value.elapsed))
const tenths = computed(() => Math.floor((status.value.elapsed * 10) % 10))

const drift = computed(() =>
  status.value.driftMs == null ? '—' : `${status.value.driftMs.toFixed(1)} ms`,
)

const buttonLabel = computed(() => {
  if (finalizing.value) return t('recorder.finalizing')
  if (recorder.busy && !recording.value) return t('recorder.starting')
  return recording.value ? t('recorder.stop') : t('recorder.start')
})

const stateTag = computed(() => {
  const s = status.value.state
  return { type: s === 'recording' ? ('error' as const) : s === 'finalizing' ? ('warning' as const) : ('default' as const), text: t(`recorder.state.${s}`) }
})

function onButton(): void {
  void recorder.toggle()
}

onMounted(() => void recorder.init())
</script>

<template>
  <div class="recorder">
    <NResult
      v-if="!recorder.available"
      status="info"
      :title="t('recorder.desktopOnly.title')"
      :description="t('recorder.desktopOnly.text')"
      data-testid="recorder-desktop-only"
    >
      <template #footer>
        <RouterLink :to="{ name: 'upload' }">
          <NButton type="primary">{{ t('recorder.desktopOnly.back') }}</NButton>
        </RouterLink>
      </template>
    </NResult>

    <NSpace v-else vertical size="large">
      <NAlert v-if="recorder.error" type="error" closable data-testid="recorder-error" @close="recorder.error = null">
        {{ recorder.error }}
      </NAlert>

      <NCard :title="t('recorder.title')">
        <template #header-extra>
          <NTag :type="stateTag.type" round data-testid="recorder-state" :data-state="status.state">
            <span v-if="recording" class="rec-dot" />{{ stateTag.text }}
          </NTag>
        </template>

        <div class="control">
          <button
            type="button"
            class="big-button"
            :class="{ recording, finalizing }"
            :disabled="finalizing || recorder.busy || (!recording && !recorder.canStart)"
            :aria-label="buttonLabel"
            data-testid="recorder-toggle"
            @click="onButton"
          >
            <span class="icon" />
          </button>
          <div class="clock-box">
            <div class="clock" data-testid="recorder-elapsed">
              {{ elapsed }}<span class="tenths">.{{ tenths }}</span>
            </div>
            <div class="button-label">{{ buttonLabel }}</div>
            <NText depth="3" class="hint">{{ t('recorder.hotkey') }}</NText>
          </div>
        </div>

        <div class="sources">
          <div class="source" :class="{ dropped: systemOff }" data-testid="source-system">
            <div class="source-head">
              <NSwitch v-model:value="recorder.options.system" :disabled="locked" data-testid="toggle-system" />
              <div>
                <div class="source-title">
                  {{ t('recorder.system') }}
                  <NTag v-if="systemOff" size="tiny" type="warning">{{ t('recorder.sourceOff') }}</NTag>
                </div>
                <NText depth="3" class="hint">{{ t('recorder.systemHint') }}</NText>
              </div>
            </div>
            <NRadioGroup
              :value="recorder.sourceMode"
              size="small"
              :disabled="locked || !recorder.options.system"
              data-testid="source-mode"
              @update:value="onSourceMode"
            >
              <NRadioButton value="device" data-testid="source-mode-device">
                {{ t('recorder.sourceDevice') }}
              </NRadioButton>
              <NRadioButton value="process" data-testid="source-mode-process">
                {{ t('recorder.sourceProcess') }}
              </NRadioButton>
            </NRadioGroup>
            <NSelect
              v-if="recorder.sourceMode === 'device'"
              v-model:value="outputDevice"
              :options="outputOptions"
              :disabled="locked || !recorder.options.system"
              :loading="recorder.loadingDevices"
              size="small"
              data-testid="device-output"
            />
            <template v-else>
              <NSpace :wrap="false" size="small" align="center">
                <NSelect
                  v-model:value="processValue"
                  :options="processOptions"
                  :disabled="locked || !recorder.options.system"
                  :loading="recorder.loadingProcesses"
                  :placeholder="t('recorder.processPlaceholder')"
                  size="small"
                  filterable
                  style="min-width: 0; flex: 1"
                  data-testid="process-select"
                />
                <NButton
                  size="small"
                  :disabled="locked"
                  :loading="recorder.loadingProcesses"
                  data-testid="process-refresh"
                  @click="recorder.refreshProcesses()"
                >
                  {{ t('recorder.refreshProcesses') }}
                </NButton>
              </NSpace>
              <NText v-if="recorder.processesError" type="error" class="hint" data-testid="process-error">
                {{ t('recorder.processUnavailable', { error: recorder.processesError }) }}
              </NText>
              <NText
                v-else-if="!recorder.loadingProcesses && !recorder.processes.length"
                depth="3"
                class="hint"
              >
                {{ t('recorder.processEmpty') }}
              </NText>
              <NText v-else depth="3" class="hint">{{ t('recorder.sourceProcessHint') }}</NText>
            </template>
            <NText v-if="capturingLabel" type="info" class="hint" data-testid="recorder-capturing">
              {{ capturingLabel }}
            </NText>
            <LevelMeter
              :level="status.levels.system"
              :active="recording && status.sources.system"
              testid="meter-system"
            />
          </div>
          <div class="source" :class="{ dropped: micOff }" data-testid="source-mic">
            <div class="source-head">
              <NSwitch v-model:value="recorder.options.mic" :disabled="locked" data-testid="toggle-mic" />
              <div>
                <div class="source-title">
                  {{ t('recorder.mic') }}
                  <NTag v-if="micOff" size="tiny" type="warning" data-testid="mic-dropped">
                    {{ t('recorder.sourceOff') }}
                  </NTag>
                </div>
                <NText depth="3" class="hint">{{ t('recorder.micHint') }}</NText>
              </div>
            </div>
            <NSelect
              v-model:value="inputDevice"
              :options="inputOptions"
              :disabled="locked || !recorder.options.mic"
              :loading="recorder.loadingDevices"
              size="small"
              data-testid="device-input"
            />
            <LevelMeter
              :level="status.levels.mic"
              :active="recording && status.sources.mic"
              testid="meter-mic"
            />
          </div>
        </div>
        <NSpace align="center" size="small" class="row-gap">
          <NButton size="tiny" quaternary :disabled="locked" @click="recorder.refreshDevices()">
            {{ t('recorder.refreshDevices') }}
          </NButton>
          <NText v-if="noSource" type="warning" data-testid="recorder-no-source">
            {{ t('recorder.needSource') }}
          </NText>
          <NText v-else-if="!recorder.active && recorder.needsProcess" type="warning" data-testid="recorder-need-process">
            {{ t('recorder.needProcess') }}
          </NText>
        </NSpace>

        <div class="details">
          <div class="detail">
            <span class="label">{{ t('recorder.outDir') }}</span>
            <NSpace vertical size="small" class="grow">
              <NInput
                v-model:value="outDir"
                :disabled="locked"
                :placeholder="t('recorder.outDirPlaceholder')"
                size="small"
                clearable
                data-testid="recorder-outdir"
              />
              <NText depth="3" class="hint">{{ t('recorder.outDirHint') }}</NText>
            </NSpace>
          </div>
          <div v-if="status.file" class="detail">
            <span class="label">{{ t('recorder.file') }}</span>
            <NText code class="path" data-testid="recorder-file">{{ status.file }}</NText>
          </div>
          <div class="detail">
            <span class="label">{{ t('recorder.drift') }}</span>
            <span class="mono" data-testid="recorder-drift">{{ drift }}</span>
          </div>
        </div>

        <NAlert
          v-if="status.warnings.length"
          type="warning"
          :title="t('recorder.warnings')"
          class="row-gap"
          data-testid="recorder-warnings"
        >
          <div v-for="(w, i) in status.warnings" :key="i">{{ w }}</div>
        </NAlert>
      </NCard>

      <NCard :title="t('recorder.list.title')">
        <template #header-extra>
          <NButton size="small" quaternary :loading="recorder.loadingRecordings" @click="recorder.refreshRecordings()">
            {{ t('recorder.list.refresh') }}
          </NButton>
        </template>
        <NAlert
          v-if="recorder.config && !recorder.config.targetDir"
          type="warning"
          :show-icon="false"
          class="list-alert"
          data-testid="recorder-no-target"
        >
          {{ t('recorder.noTargetDir') }}
          <RouterLink :to="{ name: 'settings' }">{{ t('recorder.goToSettings') }}</RouterLink>
        </NAlert>
        <NAlert
          v-else-if="recorder.config && !recorder.config.autoUpload"
          type="info"
          :show-icon="false"
          class="list-alert"
        >
          {{ t('recorder.autoUploadOff') }}
          <RouterLink :to="{ name: 'settings' }">{{ t('recorder.goToSettings') }}</RouterLink>
        </NAlert>
        <RecordingsList />
      </NCard>
    </NSpace>
  </div>
</template>

<style scoped>
.recorder {
  max-width: 960px;
  margin: 0 auto;
}

.control {
  display: flex;
  align-items: center;
  gap: 28px;
  margin-bottom: 24px;
}

.big-button {
  flex: none;
  width: 96px;
  height: 96px;
  border-radius: 50%;
  border: 4px solid rgba(208, 48, 80, 0.35);
  background: #d03050;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: transform 0.1s, box-shadow 0.2s, background-color 0.2s;
}

.big-button:hover:not(:disabled) {
  box-shadow: 0 0 0 6px rgba(208, 48, 80, 0.15);
}

.big-button:active:not(:disabled) {
  transform: scale(0.96);
}

.big-button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.big-button .icon {
  width: 34px;
  height: 34px;
  border-radius: 50%;
  background: #fff;
  transition: border-radius 0.2s, width 0.2s, height 0.2s;
}

.big-button.recording {
  animation: pulse 1.6s ease-in-out infinite;
}

.big-button.recording .icon,
.big-button.finalizing .icon {
  width: 30px;
  height: 30px;
  border-radius: 4px;
}

@keyframes pulse {
  0%,
  100% {
    box-shadow: 0 0 0 0 rgba(208, 48, 80, 0.35);
  }
  50% {
    box-shadow: 0 0 0 10px rgba(208, 48, 80, 0);
  }
}

.clock {
  font-size: 44px;
  font-weight: 600;
  line-height: 1.1;
  font-variant-numeric: tabular-nums;
}

.tenths {
  font-size: 24px;
  opacity: 0.6;
}

.button-label {
  font-size: 15px;
  margin: 2px 0 4px;
}

.hint {
  font-size: 12px;
}

.sources {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 20px;
}

.source {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px;
  border: 1px solid rgba(128, 128, 128, 0.22);
  border-radius: 6px;
}

.source-head {
  display: flex;
  gap: 10px;
  align-items: flex-start;
}

.source-title {
  font-weight: 500;
  display: flex;
  align-items: center;
  gap: 6px;
}

/* The core dropped this source (e.g. no microphone): grey it out next to the warning. */
.source.dropped {
  opacity: 0.55;
}

.row-gap {
  margin-top: 12px;
}

.details {
  margin-top: 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.detail {
  display: flex;
  gap: 12px;
  align-items: baseline;
}

.label {
  flex: none;
  width: 72px;
  opacity: 0.75;
}

.grow {
  flex: 1;
}

.path {
  word-break: break-all;
}

.mono {
  font-variant-numeric: tabular-nums;
}

.list-alert {
  margin-bottom: 12px;
}

.rec-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: currentColor;
  margin-right: 6px;
  animation: blink 1s steps(2, start) infinite;
}

@keyframes blink {
  to {
    visibility: hidden;
  }
}

@media (max-width: 720px) {
  .sources {
    grid-template-columns: minmax(0, 1fr);
  }

  .control {
    gap: 16px;
  }
}
</style>
