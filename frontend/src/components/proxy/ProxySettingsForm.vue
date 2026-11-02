<script setup lang="ts">
/**
 * Proxy controls shared by the setup wizard's "网络" step and Settings → 网络代理: what is in
 * effect now (source, masked URL, PAC warning), the mode (跟随系统 / 不使用代理 / 手动), the
 * manual URL + no-proxy list, and "测试连接" for the unsaved draft. The parent saves through the
 * exposed `save()`. Credentials are never shown: the core returns them masked, and the masked
 * URL sent back unchanged keeps the stored ones.
 */
import { NAlert, NButton, NFormItem, NInput, NRadio, NRadioGroup, NSpace, NTag, NText } from 'naive-ui'
import { computed, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import type { ProxyMode, ProxySettings, ProxyTestResult } from '@/platform'
import { proxyUrlError, sameProxySettings, useProxyStore } from '@/stores/proxy'

const { t } = useI18n()
const proxy = useProxyStore()

const draft = reactive<ProxySettings>({ ...proxy.settings })
watch(
  () => proxy.settings,
  (s) => Object.assign(draft, { ...s, url: s.url ?? '' }),
  { immediate: true },
)

const MODES: ProxyMode[] = ['system', 'none', 'manual']

const urlError = computed(() => (draft.mode === 'manual' ? proxyUrlError(draft.url) : null))
const valid = computed(() => !urlError.value)
const dirty = computed(() => !sameProxySettings(draft, proxy.settings))
/** The field still holds the core's masked URL (credentials kept). */
const masked = computed(() => !!draft.url && draft.url === proxy.settings.url && draft.url.includes('***'))

const result = ref<ProxyTestResult | null>(null)
const testError = ref<string | null>(null)
watch(draft, () => {
  result.value = null
  testError.value = null
})

async function onTest(): Promise<void> {
  result.value = null
  testError.value = null
  try {
    result.value = await proxy.test({ ...draft })
  } catch (e) {
    testError.value = (e as Error).message
  }
}

/** Save the draft when it changed. Throws the IPC error. */
async function save(): Promise<void> {
  if (dirty.value) await proxy.save({ ...draft })
}

const eff = computed(() => proxy.effective)

defineExpose({ save, valid, dirty })
</script>

<template>
  <div class="proxy-form" data-testid="proxy-form">
    <div v-if="eff" class="effective" data-testid="proxy-effective" :data-source="eff.source">
      <NText depth="3">{{ t('proxy.effective') }}</NText>
      <NTag size="small" round :type="eff.source === 'none' ? 'default' : 'info'">
        {{ t(`proxy.source.${eff.source}`) }}
      </NTag>
      <NText v-if="eff.url" code data-testid="proxy-effective-url">{{ eff.url }}</NText>
      <NText v-else depth="3">{{ t('proxy.direct') }}</NText>
    </div>
    <NText v-if="eff?.url && eff.noProxy" depth="3" class="hint block">
      {{ t('proxy.noProxyEffective', { list: eff.noProxy }) }}
    </NText>
    <NAlert v-if="eff?.pacUrl" type="warning" class="block" data-testid="proxy-pac">
      {{ t('proxy.pac', { url: eff.pacUrl }) }}
    </NAlert>
    <NAlert v-for="w in eff?.warnings ?? []" :key="w" type="warning" :show-icon="false" class="block">
      {{ w }}
    </NAlert>

    <div class="mode-row">
      <NText>{{ t('proxy.mode.label') }}</NText>
      <NRadioGroup v-model:value="draft.mode" data-testid="proxy-mode">
        <NRadio v-for="m in MODES" :key="m" :value="m" :data-testid="`proxy-mode-${m}`">
          {{ t(`proxy.mode.${m}`) }}
        </NRadio>
      </NRadioGroup>
    </div>

    <template v-if="draft.mode === 'manual'">
      <NFormItem
        :label="t('proxy.url')"
        :validation-status="urlError ? 'error' : undefined"
        :feedback="urlError ? t(`proxy.errors.${urlError}`) : masked ? t('proxy.maskedHint') : t('proxy.urlHint')"
      >
        <NInput
:value="draft.url ?? ''"
          @update:value="(v: string) => (draft.url = v)"
          :placeholder="t('proxy.urlPlaceholder')"
          :input-props="{ autocomplete: 'off', spellcheck: false }"
          data-testid="proxy-url"
        />
      </NFormItem>
      <NFormItem :label="t('proxy.noProxy')" :feedback="t('proxy.noProxyHint')">
        <NInput v-model:value="draft.noProxy" data-testid="proxy-no-proxy" />
      </NFormItem>
    </template>

    <NSpace align="center">
      <NButton :loading="proxy.testing" :disabled="!valid" data-testid="proxy-test" @click="onTest">
        {{ t('proxy.test') }}
      </NButton>
      <NText v-if="result" :type="result.ok ? 'success' : 'error'" data-testid="proxy-test-result" :data-ok="result.ok">
        {{ t(result.ok ? 'proxy.testOk' : 'proxy.testFailed', { message: result.message }) }}
      </NText>
      <NText v-else-if="testError" type="error" data-testid="proxy-test-result" data-ok="false">
        {{ t('common.labelDetail', { label: t('proxy.testError'), detail: testError }) }}
      </NText>
    </NSpace>
  </div>
</template>

<style scoped>
.effective {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px 8px;
  margin-bottom: 8px;
}

.block {
  display: block;
  margin-bottom: 12px;
}

.hint {
  font-size: 12px;
}

.mode-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px 16px;
  margin: 4px 0 16px;
}
</style>
