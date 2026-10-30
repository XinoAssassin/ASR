import type { SelectOption } from 'naive-ui'
import { computed, type ComputedRef } from 'vue'
import { useI18n } from 'vue-i18n'

import type { DeviceId } from '@/api'
import { useHardwareStore } from '@/stores/hardware'
import { deviceOptions } from '@/utils/hardware'

export type DeviceSelectOption = SelectOption & { label: string; value: DeviceId; disabled: boolean }

/**
 * NSelect options for cpu / cuda / rocm on the active backend: unavailable ones disabled, with
 * the backend's reason in the label. Backends without the hardware endpoint offer `current` only.
 */
export function useDeviceOptions(current: () => DeviceId | null | undefined): ComputedRef<DeviceSelectOption[]> {
  const { t } = useI18n()
  const hardware = useHardwareStore()
  return computed(() =>
    deviceOptions(hardware.supported ? hardware.info : null, current() ?? null).map((o) => ({
      value: o.value,
      disabled: o.disabled,
      label:
        o.disabled || o.hint
          ? t('common.withNote', {
              text: o.label,
              note: o.hint ? `${t('hardware.unavailable')}${t('common.listSep')}${o.hint}` : t('hardware.unavailable'),
            })
          : o.label,
    })),
  )
}
