/**
 * Keeps an editable form in step with server data without clobbering unsaved input: `sync()`
 * (called whenever the server data changes) re-fills the form only while it still equals what was
 * last filled in; `refill()` (called after this form's own save) always re-fills.
 */
export interface FormSync {
  /** Re-fill from the source unless the user has changed the form since the last fill. */
  sync(): void
  /** Re-fill unconditionally and take a new baseline (after this form's own save). */
  refill(): void
  /** The form differs from what was last filled in. */
  isDirty(): boolean
}

export function createFormSync(form: object, fill: () => boolean | void): FormSync {
  let baseline: string | null = null
  const snapshot = (): string => JSON.stringify(form)

  function refill(): void {
    // `fill` returns false when there was nothing to fill from (no server data yet).
    if (fill() === false) return
    baseline = snapshot()
  }

  function isDirty(): boolean {
    return baseline !== null && snapshot() !== baseline
  }

  function sync(): void {
    if (!isDirty()) refill()
  }

  return { sync, refill, isDirty }
}
