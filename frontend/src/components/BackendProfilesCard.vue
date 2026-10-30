<script setup lang="ts">
/**
 * Settings → backends: the profile list (switch / test / edit / delete, add a remote one) and
 * the active profile's URL + X-API-Key (the former single "connection" fields), all stored in
 * this browser. Changes that point the active profile at another backend go through
 * `useBackendSwitch` (unsaved edits / running uploads are confirmed first).
 */
import {
  NAlert,
  NButton,
  NCard,
  NForm,
  NFormItem,
  NInput,
  NModal,
  NPopconfirm,
  NSpace,
  NTag,
  NText,
  type ButtonProps,
  useDialog,
  useMessage,
} from 'naive-ui'
import { computed, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import { BackendUrlError, getDefaultBackendUrl } from '@/api'
import { useApiErrors } from '@/composables/useApiErrors'
import { useBackendSwitch } from '@/composables/useBackendSwitch'
import { LOCAL_PROFILE_ID, useProfilesStore, type BackendProfile, type ProfileTestResult } from '@/stores/profiles'
import { useSidecarStore } from '@/stores/sidecar'

const emit = defineEmits<{ keyChanged: [] }>()

const { t } = useI18n()
const message = useMessage()
const dialog = useDialog()
const { describe } = useApiErrors()
const profiles = useProfilesStore()
const sidecar = useSidecarStore()
const backend = useBackendSwitch()

function describeUrlError(e: BackendUrlError): string {
  return t(`settings.connection.urlInvalid.${e.reason}`, { url: e.input })
}

type TestOutcome = { ok: true; result: ProfileTestResult } | { ok: false; text: string }

function outcomeText(o: TestOutcome): string {
  if (!o.ok) return o.text
  const parts = [t('profiles.testOk', { version: o.result.health.version })]
  const k = o.result.keyAccepted
  parts.push(k === null ? t('profiles.testKeyNotRequired') : k ? t('profiles.testKeyOk') : t('profiles.testKeyBad'))
  return parts.join(t('common.listSep'))
}

function outcomeType(o: TestOutcome): 'success' | 'warning' | 'error' {
  if (!o.ok) return 'error'
  return o.result.keyAccepted === false ? 'warning' : 'success'
}

/** Test URL + key; a BackendUrlError is returned for the caller's field. */
async function runTest(url: string, key: string | null): Promise<TestOutcome | BackendUrlError> {
  try {
    return { ok: true, result: await profiles.test(url, key) }
  } catch (e) {
    if (e instanceof BackendUrlError) return e
    return { ok: false, text: describe(e) }
  }
}

type KeyChoice = 'clear' | 'keep' | 'cancel'
type TestButtonProps = ButtonProps & { 'data-testid': string }
const KEY_CLEAR_BUTTON: TestButtonProps = { type: 'warning', 'data-testid': 'key-clear-confirm' }
const KEY_KEEP_BUTTON: TestButtonProps = { 'data-testid': 'key-keep' }

/** The profile's stored API key would go to another host: clear it (default), keep it, or cancel. */
function askAboutKey(): Promise<KeyChoice> {
  return new Promise((resolve) => {
    let settled = false
    const done = (choice: KeyChoice): void => {
      if (!settled) resolve(choice)
      settled = true
    }
    dialog.warning({
      title: t('settings.connection.keyOtherHostTitle'),
      content: t('settings.connection.keyOtherHost'),
      positiveText: t('settings.connection.clearKeyAndSave'),
      negativeText: t('settings.connection.keepKey'),
      positiveButtonProps: KEY_CLEAR_BUTTON,
      negativeButtonProps: KEY_KEEP_BUTTON,
      onPositiveClick: () => done('clear'),
      onNegativeClick: () => done('keep'),
      onClose: () => done('cancel'),
      onMaskClick: () => done('cancel'),
      onAfterLeave: () => done('cancel'),
    })
  })
}

/** Ask about the key when needed; null = cancelled. Throws BackendUrlError. */
async function keyDecision(id: string, url: string, newKey?: string | null): Promise<{ clearApiKey: boolean } | null> {
  // A key typed together with the new URL is meant for it.
  if (newKey !== undefined && newKey !== (profiles.find(id)?.apiKey ?? '')) return { clearApiKey: false }
  if (!profiles.keyAtRisk(id, url)) return { clearApiKey: false }
  const choice = await askAboutKey()
  return choice === 'cancel' ? null : { clearApiKey: choice === 'clear' }
}

// --- the profile list -----------------------------------------------------------------------------

const rowTests = reactive<Record<string, TestOutcome | undefined>>({})
const rowTesting = reactive<Record<string, boolean>>({})

function rowUrl(p: BackendProfile): string {
  const effective = profiles.urlOf(p) || t('health.sameOrigin')
  if (p.id !== LOCAL_PROFILE_ID || p.url) return effective
  if (sidecar.available && sidecar.status.state === 'running' && sidecar.status.url) {
    return t('profiles.followsSidecar', { url: sidecar.status.url })
  }
  return t('profiles.defaultUrl', { url: effective })
}

async function testRow(p: BackendProfile): Promise<void> {
  rowTesting[p.id] = true
  const outcome = await runTest(profiles.urlOf(p), p.apiKey)
  rowTests[p.id] = outcome instanceof BackendUrlError ? { ok: false, text: describeUrlError(outcome) } : outcome
  rowTesting[p.id] = false
}

function removeRow(p: BackendProfile): void {
  try {
    profiles.remove(p.id)
    message.success(t('profiles.removed', { name: p.name }))
  } catch {
    message.warning(t('profiles.removeActive'))
  }
}

// --- add / edit dialog -----------------------------------------------------------------------------

const editor = reactive({
  show: false,
  id: null as string | null,
  name: '',
  url: '',
  apiKey: '',
  urlError: null as string | null,
  testing: false,
  test: null as TestOutcome | null,
  saving: false,
})
watch(
  () => editor.url,
  () => (editor.urlError = null),
)

function openAdd(): void {
  Object.assign(editor, { show: true, id: null, name: '', url: '', apiKey: '', urlError: null, test: null })
}

function openEdit(p: BackendProfile): void {
  Object.assign(editor, { show: true, id: p.id, name: p.name, url: p.url, apiKey: p.apiKey ?? '', urlError: null, test: null })
}

const editorTitle = computed(() => {
  if (!editor.id) return t('profiles.addTitle')
  const p = profiles.find(editor.id)
  return t('profiles.editTitle', { name: p ? backend.profileName(p) : '' })
})

const editorIsLocal = computed(() => editor.id === LOCAL_PROFILE_ID)

async function testEditor(): Promise<void> {
  if (!editor.url.trim() && !editorIsLocal.value) {
    editor.urlError = t('profiles.urlRequired')
    return
  }
  editor.testing = true
  editor.test = null
  const outcome = await runTest(editor.url, editor.apiKey)
  if (outcome instanceof BackendUrlError) editor.urlError = describeUrlError(outcome)
  else editor.test = outcome
  editor.testing = false
}

async function saveEditor(): Promise<void> {
  if (!editor.url.trim() && !editorIsLocal.value) {
    editor.urlError = t('profiles.urlRequired')
    return
  }
  editor.saving = true
  try {
    if (!editor.id) {
      const p = profiles.add({ name: editor.name, url: editor.url, apiKey: editor.apiKey })
      message.success(t('profiles.added', { name: p.name }))
      editor.show = false
      return
    }
    const decision = await keyDecision(editor.id, editor.url, editor.apiKey)
    if (!decision) return
    const ok = await backend.update(
      editor.id,
      { name: editor.name, url: editor.url, apiKey: editor.apiKey },
      decision,
    )
    if (ok) {
      message.success(t('settings.saved'))
      editor.show = false
      if (editor.id === profiles.activeId) syncActiveFields()
    }
  } catch (e) {
    if (!(e instanceof BackendUrlError)) throw e
    editor.urlError = describeUrlError(e)
  } finally {
    editor.saving = false
  }
}

// --- the active profile's URL + key (the former connection fields) ---------------------------------

const urlInput = ref(profiles.active.url)
const keyInput = ref(profiles.active.apiKey ?? '')
const urlError = ref<string | null>(null)
watch(urlInput, () => (urlError.value = null))
const probing = ref(false)
const probeResult = ref<TestOutcome | null>(null)
const activeIsLocal = computed(() => profiles.activeId === LOCAL_PROFILE_ID)
const defaultUrl = computed(() => {
  void profiles.activeUrl // the sidecar URL is part of the default
  return getDefaultBackendUrl()
})

function syncActiveFields(): void {
  urlInput.value = profiles.active.url
  keyInput.value = profiles.active.apiKey ?? ''
  urlError.value = null
  probeResult.value = null
}
// Another profile, or edited from outside this form (dialog, "use this backend").
watch(
  () => [profiles.activeId, profiles.active.url, profiles.active.apiKey],
  syncActiveFields,
)

async function testBackendUrl(): Promise<void> {
  probing.value = true
  probeResult.value = null
  const outcome = await runTest(urlInput.value.trim() || (activeIsLocal.value ? '' : profiles.active.url), keyInput.value)
  if (outcome instanceof BackendUrlError) urlError.value = describeUrlError(outcome)
  else probeResult.value = outcome
  probing.value = false
}

async function saveBackendUrl(): Promise<void> {
  const id = profiles.activeId
  if (!urlInput.value.trim() && !activeIsLocal.value) {
    urlError.value = t('profiles.urlRequired')
    return
  }
  try {
    const decision = await keyDecision(id, urlInput.value)
    if (!decision) return
    if (!(await backend.update(id, { url: urlInput.value }, decision))) return
  } catch (e) {
    if (!(e instanceof BackendUrlError)) throw e
    urlError.value = describeUrlError(e) // nothing was saved
    return
  }
  syncActiveFields()
  message.success(t('settings.saved'))
}

function resetBackendUrl(): void {
  urlInput.value = ''
  void saveBackendUrl()
}

async function clearApiKey(): Promise<void> {
  keyInput.value = ''
  await backend.update(profiles.activeId, { apiKey: null })
  message.success(t('settings.connection.keyCleared'))
  emit('keyChanged')
}

async function saveApiKey(): Promise<void> {
  await backend.update(profiles.activeId, { apiKey: keyInput.value })
  message.success(t('settings.saved'))
  emit('keyChanged') // e.g. the model list was empty (401) before the key was set
}
</script>

<template>
  <NCard id="backends" :title="t('profiles.title')" data-testid="backend-profiles">
    <template #header-extra>
      <NButton size="small" data-testid="profile-add" @click="openAdd">{{ t('profiles.add') }}</NButton>
    </template>
    <NText depth="3" class="intro">{{ t('profiles.intro') }}</NText>

    <div class="profile-list">
      <div
        v-for="p in profiles.profiles"
        :key="p.id"
        class="profile-row"
        data-testid="profile-row"
        :data-profile="p.id"
        :data-active="p.id === profiles.activeId"
      >
        <div class="profile-main">
          <NSpace align="center" :size="6">
            <strong>{{ backend.profileName(p) }}</strong>
            <NTag v-if="p.id === profiles.activeId" type="success" size="small" round>{{ t('profiles.active') }}</NTag>
            <NTag size="small" :type="p.apiKey ? 'info' : 'default'" :bordered="false">
              {{ p.apiKey ? t('profiles.keySet') : t('profiles.keyNone') }}
            </NTag>
          </NSpace>
          <NText depth="3" class="small" data-testid="profile-url">{{ rowUrl(p) }}</NText>
          <NText
            v-if="rowTests[p.id]"
            :type="outcomeType(rowTests[p.id]!)"
            class="small"
            data-testid="profile-test-result"
          >
            {{ outcomeText(rowTests[p.id]!) }}
          </NText>
        </div>
        <NSpace :size="6" :wrap="false">
          <NButton
            v-if="p.id !== profiles.activeId"
            size="small"
            type="primary"
            secondary
            :loading="profiles.switching"
            data-testid="profile-use"
            @click="backend.switchTo(p.id)"
          >
            {{ t('profiles.use') }}
          </NButton>
          <NButton size="small" :loading="rowTesting[p.id]" data-testid="profile-test" @click="testRow(p)">
            {{ t('profiles.test') }}
          </NButton>
          <NButton
            size="small"
            quaternary
            data-testid="profile-edit"
            @click="openEdit(p)"
          >
            {{ t('profiles.edit') }}
          </NButton>
          <NPopconfirm v-if="p.id !== LOCAL_PROFILE_ID" @positive-click="removeRow(p)">
            <template #trigger>
              <NButton
                size="small"
                quaternary
                type="error"
                :disabled="p.id === profiles.activeId"
                :title="p.id === profiles.activeId ? t('profiles.removeActive') : undefined"
                data-testid="profile-remove"
              >
                {{ t('profiles.remove') }}
              </NButton>
            </template>
            {{ t('profiles.removeConfirm', { name: p.name }) }}
          </NPopconfirm>
        </NSpace>
      </div>
    </div>

    <!-- The active profile's connection -->
    <NForm label-placement="left" label-width="140" class="active-form">
      <NFormItem :label="t('settings.connection.backendUrl')">
        <NSpace vertical style="width: 100%">
          <NSpace :wrap="false">
            <NInput
              v-model:value="urlInput"
              data-testid="backend-url-input"
              :status="urlError ? 'error' : undefined"
              :placeholder="activeIsLocal ? defaultUrl || t('health.sameOrigin') : t('profiles.urlPlaceholder')"
              :clearable="activeIsLocal"
              style="min-width: 320px"
            />
            <NButton :loading="probing" data-testid="backend-url-test" @click="testBackendUrl">
              {{ t('settings.connection.test') }}
            </NButton>
            <NButton type="primary" data-testid="backend-url-save" @click="saveBackendUrl">
              {{ t('common.save') }}
            </NButton>
            <NButton v-if="activeIsLocal && profiles.active.url" quaternary @click="resetBackendUrl">
              {{ t('settings.connection.reset') }}
            </NButton>
          </NSpace>
          <NText v-if="urlError" type="error" data-testid="backend-url-error">{{ urlError }}</NText>
          <NText v-if="activeIsLocal" depth="3">{{ t('settings.connection.backendUrlHint') }}</NText>
          <NAlert v-if="probeResult" :type="outcomeType(probeResult)" :show-icon="true" data-testid="backend-url-test-result">
            {{ outcomeText(probeResult) }}
          </NAlert>
        </NSpace>
      </NFormItem>
      <NFormItem :label="t('settings.connection.apiKey')">
        <NSpace vertical style="width: 100%">
          <NSpace :wrap="false">
            <NInput
              v-model:value="keyInput"
              data-testid="api-key-input"
              type="password"
              show-password-on="click"
              :placeholder="t('settings.connection.apiKeyPlaceholder')"
              :input-props="{ autocomplete: 'off' }"
              style="min-width: 320px"
            />
            <NButton type="primary" data-testid="api-key-save" @click="saveApiKey">
              {{ t('common.save') }}
            </NButton>
            <NButton
              v-if="profiles.active.apiKey"
              quaternary
              type="error"
              data-testid="api-key-clear"
              @click="clearApiKey"
            >
              {{ t('settings.connection.clearKey') }}
            </NButton>
          </NSpace>
          <NText depth="3">{{ t('settings.connection.apiKeyHint') }}</NText>
        </NSpace>
      </NFormItem>
    </NForm>

    <slot />

    <NModal
      v-model:show="editor.show"
      preset="card"
      :title="editorTitle"
      style="width: 560px; max-width: calc(100vw - 32px)"
      data-testid="profile-editor"
    >
      <NForm label-placement="left" label-width="90">
        <NFormItem v-if="!editorIsLocal" :label="t('profiles.name')">
          <NInput v-model:value="editor.name" :placeholder="t('profiles.namePlaceholder')" data-testid="profile-name" />
        </NFormItem>
        <NFormItem
          :label="t('profiles.url')"
          :validation-status="editor.urlError ? 'error' : undefined"
          :feedback="editor.urlError ?? undefined"
        >
          <NInput
            v-model:value="editor.url"
            :placeholder="editorIsLocal ? getDefaultBackendUrl() || t('health.sameOrigin') : t('profiles.urlPlaceholder')"
            :clearable="editorIsLocal"
            data-testid="profile-url-input"
          />
        </NFormItem>
        <NFormItem :label="t('settings.connection.apiKey')">
          <NInput
            v-model:value="editor.apiKey"
            type="password"
            show-password-on="click"
            :placeholder="t('settings.connection.apiKeyPlaceholder')"
            :input-props="{ autocomplete: 'off' }"
            data-testid="profile-key-input"
          />
        </NFormItem>
      </NForm>
      <NAlert v-if="editor.test" :type="outcomeType(editor.test)" data-testid="profile-editor-test-result">
        {{ outcomeText(editor.test) }}
      </NAlert>
      <template #footer>
        <NSpace justify="end">
          <NButton :loading="editor.testing" data-testid="profile-editor-test" @click="testEditor">
            {{ t('profiles.test') }}
          </NButton>
          <NButton @click="editor.show = false">{{ t('common.cancel') }}</NButton>
          <NButton type="primary" :loading="editor.saving" data-testid="profile-editor-save" @click="saveEditor">
            {{ t('common.save') }}
          </NButton>
        </NSpace>
      </template>
    </NModal>
  </NCard>
</template>

<style scoped>
.intro {
  display: block;
  margin-bottom: 12px;
}

.profile-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 20px;
}

.profile-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 12px;
  border: 1px solid var(--n-border-color, rgba(128, 128, 128, 0.25));
  border-radius: 6px;
}

.profile-row[data-active='true'] {
  border-color: #18a058;
}

.profile-main {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.small {
  font-size: 12px;
}
</style>
