<script setup lang="ts">
/**
 * Probe summary of an uploaded file, the detected channel policy with a one-click override
 * (→ options.preprocess.channels), an audio-track selector for multi-stream containers, and the
 * cloud size hint (D8).
 */
import {
  NAlert,
  NButton,
  NCard,
  NDescriptions,
  NDescriptionsItem,
  NSelect,
  NSpace,
  NTag,
  NText,
} from 'naive-ui'
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

import type { ChannelsOption, ProviderName, UploadOut } from '@/api'
import { estimateCloudPrep, FLAC_16K_MB_PER_HOUR_PER_CHANNEL } from '@/utils/audio'
import { formatBytes, formatDuration } from '@/utils/format'

const props = defineProps<{
  upload: UploadOut
  source: 'uploaded' | 'registered'
  provider: ProviderName
  maxFileSizeMb: number
}>()
const channels = defineModel<ChannelsOption>('channels', { required: true })
const streamIndex = defineModel<number>('streamIndex', { required: true })
defineEmits<{ remove: [] }>()

const { t } = useI18n()

const probe = computed(() => props.upload.probe ?? null)
const multiChannel = computed(() => (probe.value?.channels ?? 1) > 1)

const policyType = computed(() => {
  switch (probe.value?.channel_policy) {
    case 'per_channel':
      return 'warning' as const
    case 'downmix':
      return 'info' as const
    default:
      return 'default' as const
  }
})

const channelOptions = computed(() => {
  const detected = probe.value ? t(`channelPolicy.${probe.value.channel_policy}`) : ''
  const all: { value: ChannelsOption; label: string; disabled?: boolean }[] = [
    { value: 'auto', label: t('channelsOption.auto', { detected }) },
    { value: 'downmix', label: t('channelsOption.downmix') },
    { value: 'left', label: t('channelsOption.left'), disabled: !multiChannel.value },
    { value: 'right', label: t('channelsOption.right'), disabled: !multiChannel.value },
    { value: 'per_channel', label: t('channelsOption.per_channel'), disabled: !multiChannel.value },
  ]
  return all
})

const streamOptions = computed(() =>
  Array.from({ length: probe.value?.audio_streams ?? 1 }, (_, i) => ({
    value: i,
    label: t('upload.probe.track', { n: i + 1 }),
  })),
)

const cloudPrep = computed(() =>
  probe.value && props.provider === 'cloud'
    ? estimateCloudPrep(probe.value, channels.value, props.maxFileSizeMb)
    : null,
)
</script>

<template>
  <NCard size="small" data-testid="probe-card" :data-upload-id="upload.id">
    <template #header>
      <NSpace align="center" size="small">
        <span class="file-name">{{ upload.original_name }}</span>
        <NTag size="small" :bordered="false">
          {{ source === 'registered' ? t('upload.probe.registered') : t('upload.probe.uploaded') }}
        </NTag>
      </NSpace>
    </template>
    <template #header-extra>
      <NButton size="small" quaternary @click="$emit('remove')">{{ t('common.remove') }}</NButton>
    </template>

    <NText depth="3" class="path">{{ upload.path }}</NText>

    <template v-if="probe">
      <NDescriptions :column="3" size="small" label-placement="left" class="probe-grid">
        <NDescriptionsItem :label="t('upload.probe.format')">
          {{ probe.container }} / {{ probe.codec }}
        </NDescriptionsItem>
        <NDescriptionsItem :label="t('upload.probe.sampleRate')">
          {{ (probe.sample_rate / 1000).toFixed(probe.sample_rate % 1000 ? 2 : 0) }} kHz
        </NDescriptionsItem>
        <NDescriptionsItem :label="t('upload.probe.channels')">
          {{ probe.channels }}
        </NDescriptionsItem>
        <NDescriptionsItem :label="t('upload.probe.duration')">
          <span data-testid="probe-duration">{{ formatDuration(probe.duration) }}</span>
        </NDescriptionsItem>
        <NDescriptionsItem :label="t('upload.probe.size')">
          {{ formatBytes(probe.size) }}
        </NDescriptionsItem>
        <NDescriptionsItem :label="t('upload.probe.bitRate')">
          {{ probe.bit_rate ? `${Math.round(probe.bit_rate / 1000)} kbps` : '—' }}
        </NDescriptionsItem>
      </NDescriptions>

      <NSpace align="center" class="policy-row">
        <NText>{{ t('upload.probe.detectedPolicy') }}</NText>
        <NTag :type="policyType" size="small" data-testid="channel-policy">
          {{ t(`channelPolicy.${probe.channel_policy}`) }}
        </NTag>
        <NText v-if="probe.channel_correlation != null" depth="3">
          {{ t('upload.probe.correlation', { value: probe.channel_correlation.toFixed(2) }) }}
        </NText>
        <NSelect
          v-model:value="channels"
          :options="channelOptions"
          size="small"
          data-testid="channel-select"
          style="width: 240px"
        />
        <NSelect
          v-if="probe.audio_streams > 1"
          v-model:value="streamIndex"
          :options="streamOptions"
          size="small"
          data-testid="stream-select"
          style="width: 140px"
        />
      </NSpace>
      <NText v-if="channels === 'per_channel'" depth="3" class="hint">
        {{ t('upload.probe.perChannelHint') }}
      </NText>

      <NAlert
        v-if="cloudPrep?.transcode"
        type="info"
        :show-icon="false"
        class="hint"
        data-testid="cloud-size-hint"
      >
        {{ t('upload.probe.transcodeHint') }}
        <template v-if="cloudPrep.estimatedBytes != null">
          {{
            t('upload.probe.estimate', {
              size: formatBytes(cloudPrep.estimatedBytes),
              rate: FLAC_16K_MB_PER_HOUR_PER_CHANNEL,
            })
          }}
        </template>
        <br />
        <NText depth="3">
          {{
            cloudPrep.reason === 'size'
              ? t('upload.probe.reasonSize', { mb: maxFileSizeMb })
              : t('upload.probe.reasonDownmix')
          }}
        </NText>
      </NAlert>
    </template>
    <NAlert v-else type="warning" :show-icon="false">{{ t('upload.probe.missing') }}</NAlert>
  </NCard>
</template>

<style scoped>
.file-name {
  font-weight: 600;
  word-break: break-all;
}

.path {
  display: block;
  font-size: 12px;
  margin-bottom: 8px;
  word-break: break-all;
}

.probe-grid {
  margin-bottom: 8px;
}

.policy-row {
  margin-top: 4px;
}

.hint {
  display: block;
  margin-top: 8px;
}
</style>
