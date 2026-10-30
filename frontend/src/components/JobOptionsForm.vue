<script setup lang="ts">
/** Job options shared by every file submitted from the Upload page. */
import {
  NCheckbox,
  NCheckboxGroup,
  NForm,
  NFormItem,
  NInput,
  NInputNumber,
  NRadio,
  NRadioGroup,
  NSelect,
  NSpace,
  NSwitch,
  NText,
} from 'naive-ui'
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

import type { TermSetOut } from '@/api'
import { LANGUAGE_OPTIONS, MAX_CONTEXT_LENGTH, MAX_SPEAKERS, type JobFormState } from '@/utils/jobForm'
import { parseTerms } from '@/utils/terms'

import { speakerCountMissing } from './interaction'

const form = defineModel<JobFormState>({ required: true })
const props = defineProps<{ termSets: TermSetOut[]; termSetsError: string | null }>()

const { t } = useI18n()

const termSetOptions = computed(() =>
  props.termSets.map((s) => ({ label: `${s.name}（${s.terms.length}）`, value: s.id })),
)

const parsed = computed(() => parseTerms(form.value.extraTerms))
/** Cleared count in fixed mode; the Upload page also blocks "Start" on this. */
const speakersMissing = computed(() => speakerCountMissing(form.value))
</script>

<template>
  <NForm label-placement="left" label-width="120" data-testid="job-options">
    <NFormItem :label="t('jobOptions.provider')">
      <NRadioGroup v-model:value="form.provider" data-testid="job-provider">
        <NRadio value="cloud">{{ t('provider.cloud') }}</NRadio>
        <NRadio value="local">{{ t('provider.local') }}</NRadio>
      </NRadioGroup>
    </NFormItem>
    <NFormItem :label="t('jobOptions.languages')">
      <NCheckboxGroup v-model:value="form.languageHints" data-testid="job-languages">
        <NSpace>
          <NCheckbox v-for="lang in LANGUAGE_OPTIONS" :key="lang" :value="lang">
            {{ t(`languages.${lang}`) }}
          </NCheckbox>
        </NSpace>
      </NCheckboxGroup>
    </NFormItem>
    <NFormItem
      :label="t('jobOptions.speakers')"
      :validation-status="speakersMissing ? 'error' : undefined"
      :feedback="speakersMissing ? t('jobOptions.speakersMissing') : undefined"
    >
      <NSpace align="center">
        <NRadioGroup v-model:value="form.speakerMode" data-testid="job-speaker-mode">
          <NRadio value="auto">{{ t('jobOptions.speakersAuto') }}</NRadio>
          <NRadio value="fixed">{{ t('jobOptions.speakersFixed') }}</NRadio>
        </NRadioGroup>
        <NInputNumber
          v-if="form.speakerMode === 'fixed'"
          v-model:value="form.numSpeakers"
          :min="1"
          :max="MAX_SPEAKERS"
          size="small"
          :status="speakersMissing ? 'error' : undefined"
          data-testid="job-num-speakers"
          style="width: 110px"
        />
      </NSpace>
    </NFormItem>
    <NFormItem :label="t('jobOptions.termSets')">
      <NSpace vertical style="width: 100%">
        <NSelect
          v-model:value="form.termSetIds"
          multiple
          clearable
          :options="termSetOptions"
          :placeholder="t('jobOptions.termSetsPlaceholder')"
          data-testid="job-term-sets"
        />
        <NText v-if="termSetsError" depth="3">{{ termSetsError }}</NText>
      </NSpace>
    </NFormItem>
    <NFormItem :label="t('jobOptions.extraTerms')">
      <NSpace vertical style="width: 100%">
        <NInput
          v-model:value="form.extraTerms"
          type="textarea"
          :autosize="{ minRows: 2, maxRows: 8 }"
          :placeholder="t('jobOptions.extraTermsPlaceholder')"
          data-testid="job-extra-terms"
        />
        <NText depth="3">
          {{ t('jobOptions.extraTermsCount', { n: parsed.terms.length }) }}
          · {{ t('jobOptions.weightHint') }}
        </NText>
        <NText v-for="err in parsed.errors" :key="err.line" type="error">
          {{ t(`jobOptions.termError.${err.reason}`, { line: err.line, text: err.text }) }}
        </NText>
      </NSpace>
    </NFormItem>
    <NFormItem :label="t('jobOptions.context')">
      <NInput
        v-model:value="form.context"
        type="textarea"
        :maxlength="MAX_CONTEXT_LENGTH"
        show-count
        :autosize="{ minRows: 2, maxRows: 6 }"
        :placeholder="t('jobOptions.contextPlaceholder')"
        data-testid="job-context"
      />
    </NFormItem>
    <NFormItem :label="t('jobOptions.keepDialect')">
      <NSpace align="center">
        <NSwitch v-model:value="form.keepDialect" data-testid="job-keep-dialect" />
        <NText depth="3">{{ t('jobOptions.keepDialectHint') }}</NText>
      </NSpace>
    </NFormItem>
    <NFormItem :label="t('jobOptions.termCorrection')">
      <NSpace align="center">
        <NSwitch v-model:value="form.termCorrection" data-testid="job-term-correction" />
        <NText depth="3">{{ t('jobOptions.termCorrectionHint') }}</NText>
      </NSpace>
    </NFormItem>
    <NFormItem :label="t('jobOptions.repetitionFilter')">
      <NSpace align="center">
        <NSwitch v-model:value="form.repetitionFilter" data-testid="job-repetition-filter" />
        <NText depth="3">{{ t('jobOptions.repetitionFilterHint') }}</NText>
      </NSpace>
    </NFormItem>
    <NFormItem v-if="form.provider === 'local'" :label="t('jobOptions.peakGain')">
      <NSpace align="center">
        <NSwitch v-model:value="form.peakGain" data-testid="job-peak-gain" />
        <NText depth="3">{{ t('jobOptions.peakGainHint') }}</NText>
      </NSpace>
    </NFormItem>
  </NForm>
</template>
