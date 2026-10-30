/** Hardware (compute device) choices for the local pipeline (pure; unit-tested). */
import type { DeviceId, HardwareOut } from '@/api/endpoints'

/** In the backend's order (`HardwareOut.devices` is always cpu, cuda, rocm). */
export const DEVICE_IDS: readonly DeviceId[] = ['cpu', 'cuda', 'rocm']

export const DEVICE_LABELS: Record<DeviceId, string> = { cpu: 'CPU', cuda: 'CUDA', rocm: 'ROCm' }

export interface DeviceOption {
  value: DeviceId
  /** "ROCm · AMD Radeon 8060S" */
  label: string
  disabled: boolean
  /** Why it is unavailable (backend `detail`), shown as a hint. */
  hint: string | null
}

export function isDeviceId(value: unknown): value is DeviceId {
  return typeof value === 'string' && (DEVICE_IDS as readonly string[]).includes(value)
}

/** "ROCm · Radeon 8060S", or just "CPU" when there is no name. */
export function deviceLabel(id: DeviceId, name?: string | null): string {
  return name ? `${DEVICE_LABELS[id]} · ${name}` : DEVICE_LABELS[id]
}

/**
 * Options of a device select. `hardware` null = the backend has no hardware endpoint (older
 * version): only the current settings device is offered. The current device stays selectable
 * even when reported unavailable, so a stored choice is never silently replaced.
 */
export function deviceOptions(hardware: HardwareOut | null, current: DeviceId | null): DeviceOption[] {
  if (!hardware) {
    const id = current ?? 'cpu'
    return [{ value: id, label: DEVICE_LABELS[id], disabled: false, hint: null }]
  }
  return DEVICE_IDS.map((id) => {
    const info = hardware.devices.find((d) => d.id === id)
    const available = id === 'cpu' ? info?.available !== false : !!info?.available
    return {
      value: id,
      label: deviceLabel(id, info?.name),
      disabled: !available && id !== current,
      hint: available ? null : (info?.detail ?? null),
    }
  })
}

/** Default device of a new job: the settings device, else the backend's hardware default. */
export function defaultJobDevice(
  settingsDevice: DeviceId | null | undefined,
  hardware: HardwareOut | null,
): DeviceId | null {
  if (settingsDevice) return settingsDevice
  return hardware?.default ?? null
}

/**
 * What the top bar shows: the device local jobs run on by default plus its name from the
 * hardware endpoint, e.g. "ROCm · AMD Radeon 8060S". Null while nothing is known.
 */
export function activeHardwareLabel(
  settingsDevice: DeviceId | null | undefined,
  hardware: HardwareOut | null,
): string | null {
  const id = defaultJobDevice(settingsDevice, hardware)
  if (!id) return null
  return deviceLabel(id, hardware?.devices.find((d) => d.id === id)?.name)
}

/**
 * A job-creation 422 about the requested device: the backend answers
 * "device 'cuda' is not available: <detail>".
 */
export function isDeviceRejection(status: number, detail: string | null): boolean {
  return status === 422 && !!detail && /\bdevice\b.*\bnot available\b/i.test(detail)
}
