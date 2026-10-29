<script setup lang="ts">
/** Horizontal peak meter (dBFS scale) fed by `RecorderStatus.levels` at ~10 Hz. */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

import { formatDb, meterFraction } from './level'

const props = defineProps<{ level: number; active: boolean; testid?: string }>()
const { t } = useI18n()

const fraction = computed(() => (props.active ? meterFraction(props.level) : 0))
const label = computed(() => (props.active ? (formatDb(props.level) ?? t('recorder.silent')) : '—'))
</script>

<template>
  <div class="meter" :class="{ inactive: !active }" :data-testid="testid" :data-level="fraction.toFixed(2)">
    <div class="track" role="meter" aria-valuemin="0" aria-valuemax="1" :aria-valuenow="fraction">
      <div class="fill" :style="{ clipPath: `inset(0 ${((1 - fraction) * 100).toFixed(1)}% 0 0)` }" />
      <span class="tick" style="left: 80%" />
      <span class="tick" style="left: 95%" />
    </div>
    <span class="value">{{ label }}</span>
  </div>
</template>

<style scoped>
.meter {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.track {
  position: relative;
  flex: 1;
  height: 12px;
  border-radius: 6px;
  overflow: hidden;
  background: rgba(128, 128, 128, 0.18);
}

/* Full-width gradient clipped to the level, so colours mark absolute levels:
   green up to -12 dB (80%), yellow to -3 dB (95%), red above. */
.fill {
  position: absolute;
  inset: 0;
  background: linear-gradient(90deg, #18a058 0%, #18a058 80%, #f0a020 80%, #f0a020 95%, #d03050 95%);
  transition: clip-path 90ms linear;
}

.tick {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 1px;
  background: rgba(128, 128, 128, 0.45);
}

.value {
  width: 56px;
  text-align: right;
  font-variant-numeric: tabular-nums;
  font-size: 12px;
  opacity: 0.75;
}

.inactive .track {
  opacity: 0.45;
}
</style>
