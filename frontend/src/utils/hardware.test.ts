import { describe, expect, it } from 'vitest'

import type { HardwareOut } from '@/api/endpoints'

import { activeHardwareLabel, defaultJobDevice, deviceLabel, deviceOptions, isDeviceId, isDeviceRejection } from './hardware'

const strixHalo: HardwareOut = {
  default: 'rocm',
  devices: [
    { id: 'cpu', available: true, name: 'AMD Ryzen AI Max+ 395', detail: '16 cores' },
    { id: 'cuda', available: false, name: null, detail: 'torch 2.9.0+rocm6.4 is not a CUDA build' },
    { id: 'rocm', available: true, name: 'AMD Radeon 8060S', detail: 'gfx1151, HIP 6.4' },
  ],
}

describe('device options', () => {
  it('lists cpu / cuda / rocm with unavailable ones disabled and their reason as hint', () => {
    expect(deviceOptions(strixHalo, 'cpu')).toEqual([
      { value: 'cpu', label: 'CPU · AMD Ryzen AI Max+ 395', disabled: false, hint: null },
      { value: 'cuda', label: 'CUDA', disabled: true, hint: 'torch 2.9.0+rocm6.4 is not a CUDA build' },
      { value: 'rocm', label: 'ROCm · AMD Radeon 8060S', disabled: false, hint: null },
    ])
  })

  it('keeps the current device selectable even when it is unavailable now', () => {
    const cuda = deviceOptions(strixHalo, 'cuda').find((o) => o.value === 'cuda')!
    expect(cuda).toMatchObject({ disabled: false, hint: 'torch 2.9.0+rocm6.4 is not a CUDA build' })
  })

  it('a backend without the hardware endpoint (404) offers the current settings device only', () => {
    expect(deviceOptions(null, 'cuda')).toEqual([{ value: 'cuda', label: 'CUDA', disabled: false, hint: null }])
    expect(deviceOptions(null, null)).toEqual([{ value: 'cpu', label: 'CPU', disabled: false, hint: null }])
  })

  it('a GPU missing from the list counts as unavailable', () => {
    const opts = deviceOptions({ default: 'cpu', devices: [] }, 'cpu')
    expect(opts.map((o) => [o.value, o.disabled])).toEqual([
      ['cpu', false],
      ['cuda', true],
      ['rocm', true],
    ])
  })
})

describe('defaults and labels', () => {
  it('a job defaults to the settings device, else the hardware default', () => {
    expect(defaultJobDevice('cpu', strixHalo)).toBe('cpu')
    expect(defaultJobDevice(null, strixHalo)).toBe('rocm')
    expect(defaultJobDevice(undefined, null)).toBeNull()
  })

  it('shows the active hardware with its name', () => {
    expect(activeHardwareLabel('rocm', strixHalo)).toBe('ROCm · AMD Radeon 8060S')
    expect(activeHardwareLabel('cpu', strixHalo)).toBe('CPU · AMD Ryzen AI Max+ 395')
    expect(activeHardwareLabel('cuda', null)).toBe('CUDA')
    expect(activeHardwareLabel(null, null)).toBeNull()
    expect(deviceLabel('rocm')).toBe('ROCm')
  })

  it('recognises device ids and the backend device 422', () => {
    expect(isDeviceId('rocm')).toBe(true)
    expect(isDeviceId('npu')).toBe(false)
    expect(isDeviceRejection(422, "device 'cuda' is not available: torch is a CPU build")).toBe(true)
    expect(isDeviceRejection(422, 'num_speakers: must be <= 20')).toBe(false)
    expect(isDeviceRejection(400, "device 'cuda' is not available: x")).toBe(false)
  })
})
