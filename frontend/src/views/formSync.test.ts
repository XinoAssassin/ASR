import { reactive } from 'vue'
import { describe, expect, it } from 'vitest'

import { createFormSync } from './formSync'

function setup() {
  const server = { value: null as { model: string; device: string } | null }
  const form = reactive({ model: '', device: 'cpu' })
  const sync = createFormSync(form, () => {
    if (!server.value) return false
    form.model = server.value.model
    form.device = server.value.device
    return true
  })
  return { server, form, sync }
}

describe('createFormSync', () => {
  it('fills on first load and follows server changes while untouched', () => {
    const { server, form, sync } = setup()
    sync.sync() // no data yet
    expect(form.model).toBe('')
    server.value = { model: 'a', device: 'cpu' }
    sync.sync()
    expect(form.model).toBe('a')
    server.value = { model: 'b', device: 'cuda' }
    sync.sync()
    expect(form).toEqual({ model: 'b', device: 'cuda' })
  })

  it('keeps unsaved input when another form saves', () => {
    const { server, form, sync } = setup()
    server.value = { model: 'a', device: 'cpu' }
    sync.sync()
    form.model = 'typed'
    expect(sync.isDirty()).toBe(true)
    server.value = { model: 'a', device: 'cuda' } // the other card saved
    sync.sync()
    expect(form.model).toBe('typed')
    expect(form.device).toBe('cpu')
  })

  it('re-fills after its own save and becomes clean again', () => {
    const { server, form, sync } = setup()
    server.value = { model: 'a', device: 'cpu' }
    sync.sync()
    form.model = ''
    server.value = { model: 'default-model', device: 'cpu' } // server normalised the value
    sync.refill()
    expect(form.model).toBe('default-model')
    expect(sync.isDirty()).toBe(false)
    server.value = { model: 'c', device: 'cpu' }
    sync.sync()
    expect(form.model).toBe('c')
  })

  it('becomes clean again when the user types the value back', () => {
    const { server, form, sync } = setup()
    server.value = { model: 'a', device: 'cpu' }
    sync.sync()
    form.model = 'x'
    form.model = 'a'
    expect(sync.isDirty()).toBe(false)
  })
})
