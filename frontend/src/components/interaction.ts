import { codePointLength, utf16ToCodePoint } from '@/utils/text'

/**
 * Small DOM-light helpers shared by the proofreading UI (TranscriptView, SegmentRow, SpeakerPanel,
 * CorrectionsPanel): IME-safe key handling, which keydown targets own the keyboard, search match
 * stepping. Kept free of component state so they can be unit-tested without a DOM.
 */

/** Minimal shape of a KeyboardEvent used here (tests pass plain objects). */
export interface KeyLike {
  key: string
  code?: string
  keyCode?: number
  isComposing?: boolean
  ctrlKey?: boolean
  metaKey?: boolean
  altKey?: boolean
  shiftKey?: boolean
  defaultPrevented?: boolean
}

/**
 * True while an input method is composing (Chinese pinyin etc.): the Enter / Escape that confirms
 * or cancels a candidate must not also commit, split or close the editor. Chrome reports
 * keyCode 229 for the confirming keydown in some IMEs where `isComposing` is already false.
 */
export function isImeKey(e: Pick<KeyLike, 'isComposing' | 'keyCode'>): boolean {
  return !!e.isComposing || e.keyCode === 229
}

/**
 * Elements that handle keys themselves: text fields, buttons, links, ARIA widgets, anything
 * focusable via Tab, and Naive UI's select / dropdown / popselect menus.
 */
export const INTERACTIVE_SELECTOR = [
  'input',
  'textarea',
  'select',
  'button',
  'a',
  '[contenteditable]',
  '[role="textbox"]',
  '[role="button"]',
  '[role="switch"]',
  '[role="slider"]',
  '[role="checkbox"]',
  '[role="radio"]',
  '[role="combobox"]',
  '[role="listbox"]',
  '[role="option"]',
  '[role="menu"]',
  '[role="menuitem"]',
  '[role="tab"]',
  '[tabindex]:not([tabindex="-1"])',
  '.n-base-selection',
  '.n-base-select-menu',
  '.n-dropdown-menu',
].join(', ')

/** Naive UI popups that listen to the keyboard on `document` while they are open. */
export const POPUP_SELECTOR = '.n-dropdown-menu, .n-base-select-menu, .n-popconfirm'

/** Vue transition classes of a popup that is already closing. */
const LEAVING = /-leave-(?:active|to)\b/

/**
 * Whether one of those popups is shown. Closed popovers can stay in the DOM with
 * `display: none` (v-show) or in their leave transition; neither counts as open.
 */
export function isPopupOpen(root: Pick<Document, 'querySelectorAll'> = document): boolean {
  return Array.from(root.querySelectorAll(POPUP_SELECTOR)).some(
    (el) => el.getClientRects().length > 0 && !LEAVING.test(el.className),
  )
}

interface ElementLike {
  isContentEditable?: boolean
  closest(selector: string): unknown
}

export function isInteractiveTarget(target: unknown): boolean {
  if (!target || typeof (target as ElementLike).closest !== 'function') return false
  const el = target as ElementLike
  if (el.isContentEditable) return true
  return !!el.closest(INTERACTIVE_SELECTOR)
}

export type ShortcutAction = 'search' | 'playPause' | 'prev' | 'next' | 'edit'

/**
 * Global transcript shortcut for a keydown, or null when the key belongs to someone else: an IME
 * composition, a widget that already handled it (`defaultPrevented`), a focused interactive
 * element, or an open popup menu. Ctrl/Cmd+F always opens the search box.
 */
export function shortcutAction(
  e: KeyLike,
  ctx: { interactiveTarget: boolean; popupOpen: boolean },
): ShortcutAction | null {
  if (isImeKey(e) || e.defaultPrevented) return null
  if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === 'f') return 'search'
  if (e.ctrlKey || e.metaKey || e.altKey || ctx.interactiveTarget || ctx.popupOpen) return null
  if (e.code === 'Space' || e.key === ' ') return 'playPause'
  if (e.key === 'ArrowLeft') return 'prev'
  if (e.key === 'ArrowRight') return 'next'
  if (e.key === 'Enter') return 'edit'
  return null
}

// --- search -------------------------------------------------------------------------------------

/** Keep a match index inside [0, count) (0 when there are no matches). */
export function clampMatchIndex(index: number, count: number): number {
  if (count <= 0) return 0
  return Math.min(Math.max(0, index), count - 1)
}

/**
 * Next match index for ↑ / ↓ / Enter. Until the current match has been shown once (`revealed`),
 * a forward step shows the current match instead of skipping it, so the first Enter lands on 1/n.
 */
export function nextMatchIndex(index: number, delta: number, count: number, revealed: boolean): number {
  if (count <= 0) return 0
  const current = clampMatchIndex(index, count)
  if (!revealed && delta > 0) return current
  return (((current + delta) % count) + count) % count
}

/** "i/n" counter text that can never show an index past the total. */
export function matchCounter(index: number, count: number): string {
  return count > 0 ? `${clampMatchIndex(index, count) + 1}/${count}` : '0/0'
}

// --- job options --------------------------------------------------------------------------------

/**
 * "Fixed number of speakers" with an empty / invalid count. NInputNumber writes `null` when the
 * field is cleared, which the request builder would otherwise round up to 1 speaker.
 */
export function speakerCountMissing(form: { speakerMode: string; numSpeakers: number | null }): boolean {
  if (form.speakerMode !== 'fixed') return false
  const n = form.numSpeakers
  return typeof n !== 'number' || !Number.isFinite(n) || n < 1
}

// --- split --------------------------------------------------------------------------------------

/**
 * Split position for the backend (`SplitOp.at`, a code-point offset) from a textarea caret
 * (`selectionStart`, UTF-16), or null when the caret is at either end / unknown.
 */
export function splitOffset(text: string, selectionStart: number | null | undefined): number | null {
  if (selectionStart == null || selectionStart < 0) return null
  const at = utf16ToCodePoint(text, selectionStart)
  return at > 0 && at < codePointLength(text) ? at : null
}
