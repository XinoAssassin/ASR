<script setup lang="ts">
/**
 * First-run setup guide (checklist drawer). Every step shows done / not done from real state
 * (`useSetupGuideStore`); links go to the place that fixes it (Settings anchors, term sets,
 * recorder) and close the drawer; the top bar help button brings it back. On the desktop
 * "desktop + backend" edition the backend step hands off to the managed-backend wizard instead of
 * duplicating it.
 */
import {
  NAlert,
  NButton,
  NDrawer,
  NDrawerContent,
  NProgress,
  NSpace,
  NSpin,
  NTag,
  NText,
} from 'naive-ui'
import { computed, nextTick } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter, type RouteLocationRaw } from 'vue-router'

import { useApiErrors } from '@/composables/useApiErrors'
import { isDesktop } from '@/platform'
import { useHealthStore } from '@/stores/health'
import { useManagedBackendStore } from '@/stores/managedBackend'
import { LOCAL_PROFILE_ID, useProfilesStore } from '@/stores/profiles'
import { useSetupGuideStore, type StepId } from '@/stores/setupGuide'
import { useTermSetsStore } from '@/stores/termSets'

const { t, te } = useI18n()
const router = useRouter()
const { describe } = useApiErrors()
const guide = useSetupGuideStore()
const health = useHealthStore()
const profiles = useProfilesStore()
const termSets = useTermSetsStore()
const managed = useManagedBackendStore()
const desktop = isDesktop()

const show = computed({
  get: () => guide.open,
  set: (v: boolean) => (v ? guide.openGuide() : guide.close()),
})

const rec = computed(() => guide.recognition)
const providerName = (p: 'local' | 'cloud' | null) => (p ? t(`provider.${p}`) : '—')

/** Desktop + backend edition without a working environment: the wizard sets it up. */
const useWizard = computed(() => desktop && managed.bundled && managed.needsSetup)

const backendDetail = computed(() => {
  const name = profiles.active.id === LOCAL_PROFILE_ID ? t('profiles.local') : profiles.active.name
  const url = profiles.activeUrl || t('health.sameOrigin')
  if (health.state === 'connected' && !health.unauthorized) {
    return t('guide.backend.connected', { name, url, version: health.info?.version ?? '?' })
  }
  if (health.unauthorized) return t('guide.backend.unauthorized', { name })
  if (health.state === 'error') return t('guide.backend.failed', { name, url, error: health.error ?? '' })
  return t('guide.backend.checking', { url })
})

const missingText = computed(() =>
  rec.value.missing
    .map((m) => (m.size_mb ? t('common.withNote', { text: m.id, note: `${m.size_mb} MB` }) : m.id))
    .join(t('common.enumSep')),
)

const sampleStage = computed(() => {
  const j = guide.job
  if (!j) return ''
  const stage = te(`stage.${j.stage}`) ? t(`stage.${j.stage}`) : j.stage
  return j.status === 'queued' ? t('jobStatus.queued') : stage
})

const samplePercent = computed(() => {
  if (guide.phase === 'uploading') return Math.round(guide.uploadProgress * 100)
  if (guide.phase === 'running' && guide.job) return Math.round(Math.min(1, Math.max(0, guide.job.progress)) * 100)
  return null
})

const sampleFailure = computed(() => {
  if (guide.sampleError) return describe(guide.sampleError)
  return guide.job?.error || guide.job?.message || t('guide.sample.failedUnknown')
})

const sampleBlocked = computed(() => {
  if (guide.steps.backend !== 'done') return t('guide.sample.needBackend')
  if (!rec.value.sampleProvider) return t('guide.sample.needRecognition')
  return null
})

async function go(to: RouteLocationRaw, anchor?: string): Promise<void> {
  guide.close()
  await router.push(to)
  // Already on that page (no remount to read the hash): scroll ourselves.
  if (anchor) {
    await nextTick()
    document.getElementById(anchor)?.scrollIntoView({ block: 'start', behavior: 'smooth' })
  }
}

function goSettings(anchor: string): void {
  void go({ name: 'settings', hash: `#${anchor}` }, anchor)
}

function openWizard(): void {
  guide.close()
  managed.openWizard()
}

function openTranscript(): void {
  const id = guide.transcriptId
  if (id) void go({ name: 'transcript', params: { id } })
}

function stepClass(id: StepId): string {
  return `step step-${guide.steps[id]}`
}
</script>

<template>
  <NDrawer v-model:show="show" width="min(440px, 100vw)" placement="right" class="setup-guide" data-testid="setup-guide">
    <NDrawerContent closable :native-scrollbar="false" body-content-class="guide-body">
      <template #header>
        <div class="head">
          <span>{{ t('guide.title') }}</span>
          <NTag size="small" round :type="guide.complete ? 'success' : 'default'" data-testid="guide-progress">
            {{ t('guide.progress', { done: guide.doneCount, total: guide.visibleSteps.length }) }}
          </NTag>
        </div>
      </template>

      <NText depth="3" class="intro">{{ t('guide.intro') }}</NText>
      <NAlert v-if="guide.complete" type="success" :show-icon="false" class="complete" data-testid="guide-complete">
        {{ t('guide.complete') }}
      </NAlert>

      <ol class="steps">
        <!-- (a) backend -->
        <li :class="stepClass('backend')" data-testid="guide-step-backend" :data-state="guide.steps.backend">
          <span class="mark" aria-hidden="true">
            <NSpin v-if="guide.steps.backend === 'pending'" :size="14" />
            <template v-else-if="guide.steps.backend === 'done'">✓</template>
            <template v-else>1</template>
          </span>
          <div class="content">
            <div class="step-title">{{ t('guide.backend.title') }}</div>
            <NText depth="3" class="detail" data-testid="guide-backend-detail">{{ backendDetail }}</NText>
            <NSpace v-if="guide.steps.backend !== 'done'" size="small" class="actions">
              <NButton v-if="useWizard" size="small" type="primary" data-testid="guide-open-wizard" @click="openWizard">
                {{ t('guide.backend.wizard') }}
              </NButton>
              <NButton v-else size="small" type="primary" data-testid="guide-backend-settings" @click="goSettings('backends')">
                {{ t('guide.backend.settings') }}
              </NButton>
              <NButton size="small" quaternary @click="guide.refresh()">{{ t('guide.recheck') }}</NButton>
            </NSpace>
            <NText v-if="useWizard && guide.steps.backend !== 'done'" depth="3" class="detail">
              {{ t('guide.backend.wizardHint') }}
            </NText>
          </div>
        </li>

        <!-- (b) recognition -->
        <li :class="stepClass('recognition')" data-testid="guide-step-recognition" :data-state="guide.steps.recognition">
          <span class="mark" aria-hidden="true">
            <NSpin v-if="guide.steps.recognition === 'pending'" :size="14" />
            <template v-else-if="guide.steps.recognition === 'done'">✓</template>
            <template v-else>2</template>
          </span>
          <div class="content">
            <div class="step-title">{{ t('guide.recognition.title') }}</div>
            <template v-if="guide.steps.backend === 'done'">
              <div class="facts">
                <div data-testid="guide-local-state">
                  <NTag size="tiny" :type="rec.localReady ? 'success' : 'default'" :bordered="false">
                    {{ t('provider.local') }}
                  </NTag>
                  <NText depth="3" class="detail">
                    {{
                      rec.localReady
                        ? t('guide.recognition.localReady')
                        : !rec.localAvailable
                          ? t('guide.recognition.localUnavailable')
                          : t('guide.recognition.localMissing', { models: missingText })
                    }}
                  </NText>
                </div>
                <div data-testid="guide-cloud-state">
                  <NTag size="tiny" :type="rec.cloudReady ? 'success' : 'default'" :bordered="false">
                    {{ t('provider.cloud') }}
                  </NTag>
                  <NText depth="3" class="detail">
                    {{ rec.cloudReady ? t('guide.recognition.cloudReady') : t('guide.recognition.cloudMissing') }}
                  </NText>
                </div>
              </div>
              <NText v-if="rec.sampleProvider" depth="3" class="detail">
                {{ t('guide.recognition.default', { provider: providerName(rec.defaultProvider) }) }}
              </NText>
              <NSpace v-if="(!rec.localReady && rec.localAvailable) || !rec.cloudReady" size="small" class="actions">
                <NButton
                  v-if="!rec.localReady && rec.localAvailable"
                  size="small"
                  :type="guide.steps.recognition === 'done' ? 'default' : 'primary'"
                  data-testid="guide-models"
                  @click="goSettings('local-models')"
                >
                  {{ t('guide.recognition.download') }}
                </NButton>
                <NButton
                  v-if="!rec.cloudReady"
                  size="small"
                  :secondary="guide.steps.recognition !== 'done'"
                  data-testid="guide-cloud"
                  @click="goSettings('online')"
                >
                  {{ t('guide.recognition.setKey') }}
                </NButton>
              </NSpace>
            </template>
            <NText v-else depth="3" class="detail">{{ t('guide.recognition.waiting') }}</NText>
          </div>
        </li>

        <!-- (c) sample meeting -->
        <li :class="stepClass('sample')" data-testid="guide-step-sample" :data-state="guide.steps.sample">
          <span class="mark" aria-hidden="true">
            <NSpin v-if="guide.steps.sample === 'pending'" :size="14" />
            <template v-else-if="guide.steps.sample === 'done'">✓</template>
            <template v-else>3</template>
          </span>
          <div class="content">
            <div class="step-title">{{ t('guide.sample.title') }}</div>
            <NText depth="3" class="detail">{{ t('guide.sample.text') }}</NText>
            <template v-if="guide.phase === 'idle' || guide.phase === 'failed'">
              <NAlert v-if="guide.phase === 'failed'" type="error" :show-icon="false" class="inline-alert" data-testid="guide-sample-error">
                {{ t('common.labelDetail', { label: t('guide.sample.failed'), detail: sampleFailure }) }}
              </NAlert>
              <NSpace size="small" align="center" class="actions">
                <NButton
                  size="small"
                  type="primary"
                  :disabled="!guide.canRunSample"
                  data-testid="guide-run-sample"
                  @click="guide.runSample()"
                >
                  {{ guide.phase === 'failed' ? t('guide.sample.retry') : t('guide.sample.run') }}
                </NButton>
                <NText v-if="sampleBlocked" depth="3" class="detail">{{ sampleBlocked }}</NText>
                <NText v-else depth="3" class="detail" data-testid="guide-sample-provider">
                  {{ t('guide.sample.with', { provider: providerName(rec.sampleProvider) }) }}
                </NText>
              </NSpace>
            </template>
            <div v-else-if="guide.phase !== 'succeeded'" class="progress" data-testid="guide-sample-progress">
              <NText class="detail">
                {{
                  guide.phase === 'running'
                    ? t('guide.sample.running', { stage: sampleStage })
                    : t(`guide.sample.phase.${guide.phase}`)
                }}
              </NText>
              <NProgress
                type="line"
                :percentage="samplePercent ?? 0"
                :show-indicator="samplePercent !== null"
                :processing="samplePercent === null"
                :height="6"
              />
            </div>
            <NSpace v-else size="small" align="center" class="actions">
              <NButton size="small" type="primary" data-testid="guide-open-transcript" @click="openTranscript">
                {{ t('guide.sample.open') }}
              </NButton>
              <NButton size="small" quaternary :disabled="!guide.canRunSample" @click="guide.runSample()">
                {{ t('guide.sample.again') }}
              </NButton>
            </NSpace>
          </div>
        </li>

        <!-- (d) term set (optional) -->
        <li :class="stepClass('terms')" data-testid="guide-step-terms" :data-state="guide.steps.terms">
          <span class="mark" aria-hidden="true">
            <template v-if="guide.steps.terms === 'done'">✓</template>
            <template v-else>4</template>
          </span>
          <div class="content">
            <div class="step-title">
              {{ t('guide.terms.title') }}
              <NTag size="tiny" :bordered="false">{{ t('guide.optional') }}</NTag>
            </div>
            <NText depth="3" class="detail">
              {{ guide.steps.terms === 'done' ? t('guide.terms.done', { n: termSets.items.length }) : t('guide.terms.text') }}
            </NText>
            <NSpace size="small" class="actions">
              <NButton size="small" data-testid="guide-terms" @click="go({ name: 'term-sets' })">
                {{ guide.steps.terms === 'done' ? t('guide.terms.manage') : t('guide.terms.create') }}
              </NButton>
            </NSpace>
          </div>
        </li>

        <!-- (e) desktop: recording -->
        <li v-if="desktop" :class="stepClass('recorder')" data-testid="guide-step-recorder" :data-state="guide.steps.recorder">
          <span class="mark" aria-hidden="true">
            <template v-if="guide.steps.recorder === 'done'">✓</template>
            <template v-else>5</template>
          </span>
          <div class="content">
            <div class="step-title">
              {{ t('guide.recorder.title') }}
              <NTag size="tiny" :bordered="false">{{ t('guide.optional') }}</NTag>
            </div>
            <ul class="explain">
              <li>{{ t('guide.recorder.sources') }}</li>
              <li>{{ t('guide.recorder.autoStop') }}</li>
              <li>{{ t('guide.recorder.proxy') }}</li>
            </ul>
            <NSpace size="small" class="actions">
              <NButton size="small" data-testid="guide-recorder" @click="go({ name: 'recorder' })">
                {{ t('guide.recorder.open') }}
              </NButton>
              <NButton size="small" quaternary data-testid="guide-proxy" @click="goSettings('proxy')">
                {{ t('guide.recorder.proxyLink') }}
              </NButton>
            </NSpace>
          </div>
        </li>
      </ol>

      <template #footer>
        <NSpace justify="space-between" style="width: 100%">
          <NText depth="3" class="detail">{{ t('guide.reopenHint') }}</NText>
          <NSpace size="small">
            <NButton size="small" quaternary data-testid="guide-dismiss" @click="guide.dismiss()">
              {{ t('guide.dismiss') }}
            </NButton>
            <NButton size="small" data-testid="guide-skip" @click="guide.skip()">
              {{ guide.complete ? t('common.close') : t('guide.skip') }}
            </NButton>
          </NSpace>
        </NSpace>
      </template>
    </NDrawerContent>
  </NDrawer>
</template>

<style scoped>
.head {
  display: flex;
  align-items: center;
  gap: 10px;
}

.intro {
  display: block;
  font-size: 13px;
  margin-bottom: 12px;
}

.complete {
  margin-bottom: 12px;
}

.steps {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.step {
  display: flex;
  gap: 12px;
  align-items: flex-start;
}

.mark {
  flex: none;
  width: 24px;
  height: 24px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 600;
  border: 1.5px solid rgba(128, 128, 128, 0.45);
  color: rgba(128, 128, 128, 0.95);
}

.step-done .mark {
  border-color: #18a058;
  background: #18a058;
  color: #fff;
}

.step-pending .mark {
  border-color: #2080f0;
}

.content {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.step-title {
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 6px;
}

.detail {
  font-size: 12px;
  word-break: break-word;
}

.facts {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.facts .detail {
  margin-left: 6px;
}

.actions {
  margin-top: 4px;
}

.inline-alert {
  margin-top: 4px;
  font-size: 12px;
}

.progress {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 4px;
}

.explain {
  margin: 0;
  padding-left: 18px;
  font-size: 12px;
  line-height: 1.6;
  opacity: 0.85;
}
</style>
