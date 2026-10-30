/**
 * UI side of a backend change (top-bar switcher, Settings profile manager, sidecar card): asks
 * the user about unsaved transcript edits / running uploads, leaves routes that point at ids of
 * the previous backend, and reports desktop sync failures.
 */
import { type ButtonProps, useDialog, useMessage } from 'naive-ui'
import { useI18n } from 'vue-i18n'
import { isNavigationFailure, useRoute, useRouter } from 'vue-router'

import {
  LOCAL_PROFILE_ID,
  type BackendProfile,
  type ChangeOptions,
  type ProfileInput,
  type SwitchConcern,
  useProfilesStore,
} from '@/stores/profiles'
import { useUploadsStore } from '@/stores/uploads'

/** Routes whose params are ids on one backend. */
const BACKEND_ROUTES: Record<string, string> = { transcript: 'jobs' }

type TestButtonProps = ButtonProps & { 'data-testid': string }
const CONFIRM_BUTTON: TestButtonProps = { type: 'warning', 'data-testid': 'switch-confirm' }
const CANCEL_BUTTON: TestButtonProps = { 'data-testid': 'switch-cancel' }

export function useBackendSwitch() {
  const { t } = useI18n()
  const dialog = useDialog()
  const message = useMessage()
  const route = useRoute()
  const router = useRouter()
  const profiles = useProfilesStore()

  function profileName(p: BackendProfile): string {
    return p.id === LOCAL_PROFILE_ID ? t('profiles.local') : p.name
  }

  function confirm(concern: SwitchConcern): Promise<boolean> {
    return new Promise((resolve) => {
      let settled = false
      const done = (ok: boolean): void => {
        if (!settled) resolve(ok)
        settled = true
      }
      dialog.warning({
        title: t(`profiles.concern.${concern}.title`),
        content: t(`profiles.concern.${concern}.content`),
        positiveText: t(`profiles.concern.${concern}.confirm`),
        negativeText: t('common.cancel'),
        positiveButtonProps: CONFIRM_BUTTON,
        negativeButtonProps: CANCEL_BUTTON,
        onPositiveClick: () => done(true),
        onNegativeClick: () => done(false),
        onClose: () => done(false),
        onMaskClick: () => done(false),
        onAfterLeave: () => done(false),
      })
    })
  }

  const options: ChangeOptions = { confirm }

  /**
   * Before a backend change: confirm running uploads, then leave a page about the previous
   * backend's records (the transcript page's own guard saves / asks about unsaved edits).
   * Returns the options for the store (concerns already confirmed are not asked again), or null
   * when the user cancelled.
   */
  async function prepare(): Promise<ChangeOptions | null> {
    const accepted = new Set<SwitchConcern>()
    const uploads = useUploadsStore()
    if (uploads.uploading || uploads.submitting) {
      if (!(await confirm('uploadRunning'))) return null
      accepted.add('uploadRunning')
    }
    const target = BACKEND_ROUTES[String(route.name ?? '')]
    if (target) {
      const failure = await router.push({ name: target })
      if (isNavigationFailure(failure)) return null
    }
    return { confirm: (c) => (accepted.has(c) ? Promise.resolve(true) : confirm(c)) }
  }

  function reportDesktop(): void {
    if (profiles.desktopError) {
      message.error(
        t('common.labelDetail', { label: t('recorder.settings.syncFailed'), detail: profiles.desktopError }),
      )
    }
  }

  async function switchTo(id: string): Promise<boolean> {
    if (id === profiles.activeId || profiles.switching) return false
    const opts = await prepare()
    if (!opts || !(await profiles.switchTo(id, opts))) return false
    const p = profiles.find(id)
    if (p) message.success(t('profiles.switched', { name: profileName(p) }))
    reportDesktop()
    return true
  }

  /**
   * `profiles.update` with the confirmations (only asked when the active profile moves to
   * another origin); throws BackendUrlError.
   */
  async function update(
    id: string,
    patch: Partial<ProfileInput>,
    extra: { clearApiKey?: boolean } = {},
  ): Promise<boolean> {
    let opts: ChangeOptions | null = options
    if (id === profiles.activeId && patch.url !== undefined && profiles.changesBackend(id, patch.url)) {
      opts = await prepare()
      if (!opts) return false
    }
    const ok = await profiles.update(id, patch, { ...opts, ...extra })
    if (ok) reportDesktop()
    return ok
  }

  async function useSidecar(): Promise<boolean> {
    const opts = profiles.isLocalActive && !profiles.find(LOCAL_PROFILE_ID)?.url ? options : await prepare()
    if (!opts) return false
    const ok = await profiles.useSidecar(opts)
    if (ok) reportDesktop()
    return ok
  }

  return { switchTo, update, useSidecar, profileName }
}
