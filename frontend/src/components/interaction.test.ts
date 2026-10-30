import { describe, expect, it } from 'vitest'

import {
  INTERACTIVE_SELECTOR,
  clampMatchIndex,
  isImeKey,
  isInteractiveTarget,
  isPopupOpen,
  matchCounter,
  nextMatchIndex,
  shortcutAction,
  speakerCountMissing,
  splitOffset,
} from './interaction'

const idle = { interactiveTarget: false, popupOpen: false }

/** Fake element whose `closest` answers for the given matching selector parts. */
function el(matches: string[], isContentEditable = false) {
  return {
    isContentEditable,
    closest: (selector: string) =>
      selector.split(', ').some((part) => matches.includes(part)) ? {} : null,
  }
}

describe('isImeKey', () => {
  it('detects composition and the 229 keyCode', () => {
    expect(isImeKey({ isComposing: true, keyCode: 13 })).toBe(true)
    expect(isImeKey({ isComposing: false, keyCode: 229 })).toBe(true)
    expect(isImeKey({ isComposing: false, keyCode: 13 })).toBe(false)
  })
})

describe('isInteractiveTarget', () => {
  it('covers Naive selects / menus and tabbable widgets', () => {
    for (const part of ['.n-base-selection', '.n-dropdown-menu', '[tabindex]:not([tabindex="-1"])']) {
      expect(INTERACTIVE_SELECTOR.split(', ')).toContain(part)
      expect(isInteractiveTarget(el([part]))).toBe(true)
    }
    expect(isInteractiveTarget(el(['input']))).toBe(true)
    expect(isInteractiveTarget(el([], true))).toBe(true)
  })

  it('treats plain elements and non-elements as not interactive', () => {
    expect(isInteractiveTarget(el([]))).toBe(false)
    expect(isInteractiveTarget(null)).toBe(false)
    expect(isInteractiveTarget({})).toBe(false) // e.g. window: no closest()
  })
})

describe('shortcutAction', () => {
  it('maps the global keys', () => {
    expect(shortcutAction({ key: ' ', code: 'Space' }, idle)).toBe('playPause')
    expect(shortcutAction({ key: 'ArrowLeft' }, idle)).toBe('prev')
    expect(shortcutAction({ key: 'ArrowRight' }, idle)).toBe('next')
    expect(shortcutAction({ key: 'Enter' }, idle)).toBe('edit')
    expect(shortcutAction({ key: 'f', ctrlKey: true }, idle)).toBe('search')
    expect(shortcutAction({ key: 'F', metaKey: true }, { interactiveTarget: true, popupOpen: true })).toBe(
      'search',
    )
  })

  it('leaves keys a widget already handled alone', () => {
    expect(shortcutAction({ key: 'Enter', defaultPrevented: true }, idle)).toBeNull()
    expect(shortcutAction({ key: ' ', code: 'Space', defaultPrevented: true }, idle)).toBeNull()
    expect(shortcutAction({ key: 'f', ctrlKey: true, defaultPrevented: true }, idle)).toBeNull()
  })

  it('ignores focused widgets, open popups, modifiers and IME', () => {
    expect(shortcutAction({ key: 'Enter' }, { interactiveTarget: true, popupOpen: false })).toBeNull()
    expect(shortcutAction({ key: 'ArrowDown' }, idle)).toBeNull()
    expect(shortcutAction({ key: 'ArrowLeft' }, { interactiveTarget: false, popupOpen: true })).toBeNull()
    expect(shortcutAction({ key: 'Enter', altKey: true }, idle)).toBeNull()
    expect(shortcutAction({ key: 'Enter', isComposing: true }, idle)).toBeNull()
    expect(shortcutAction({ key: 'Enter', keyCode: 229 }, idle)).toBeNull()
  })
})

describe('search match stepping', () => {
  it('first forward step shows match 1 instead of skipping it', () => {
    expect(nextMatchIndex(0, 1, 4, false)).toBe(0)
    expect(nextMatchIndex(0, 1, 4, true)).toBe(1)
    expect(nextMatchIndex(3, 1, 4, true)).toBe(0)
  })

  it('backward steps wrap', () => {
    expect(nextMatchIndex(0, -1, 4, false)).toBe(3)
    expect(nextMatchIndex(0, -1, 4, true)).toBe(3)
  })

  it('clamps an index left over from a longer match list', () => {
    expect(clampMatchIndex(4, 4)).toBe(3)
    expect(clampMatchIndex(2, 0)).toBe(0)
    expect(nextMatchIndex(7, 1, 4, true)).toBe(0)
    expect(matchCounter(4, 4)).toBe('4/4')
    expect(matchCounter(0, 0)).toBe('0/0')
    expect(matchCounter(1, 3)).toBe('2/3')
  })
})

describe('speakerCountMissing', () => {
  it('flags a cleared count only in fixed mode', () => {
    expect(speakerCountMissing({ speakerMode: 'fixed', numSpeakers: null })).toBe(true)
    expect(speakerCountMissing({ speakerMode: 'fixed', numSpeakers: 0 })).toBe(true)
    expect(speakerCountMissing({ speakerMode: 'fixed', numSpeakers: 3 })).toBe(false)
    expect(speakerCountMissing({ speakerMode: 'auto', numSpeakers: null })).toBe(false)
  })
})

describe('splitOffset', () => {
  it('converts the UTF-16 caret to a code-point offset', () => {
    const text = '😀你好世界'
    const caret = text.indexOf('世') // 4 UTF-16 units: the emoji is a surrogate pair
    expect(caret).toBe(4)
    expect(splitOffset(text, caret)).toBe(3)
    expect([...text].slice(0, 3).join('')).toBe('😀你好')
  })

  it('refuses the ends and an unknown caret', () => {
    const text = '😀你好世界'
    expect(splitOffset(text, 0)).toBeNull()
    expect(splitOffset(text, text.length)).toBeNull()
    expect(splitOffset(text, 1)).toBeNull() // inside the emoji = before it = start
    expect(splitOffset(text, null)).toBeNull()
    expect(splitOffset(text, undefined)).toBeNull()
    expect(splitOffset('ab', 1)).toBe(1)
  })
})

describe('isPopupOpen', () => {
  const root = (els: [number, string][]) => ({
    querySelectorAll: () =>
      els.map(([n, className]) => ({ className, getClientRects: () => ({ length: n }) })),
  })
  it('ignores popovers hidden with display: none or already leaving', () => {
    expect(isPopupOpen(root([]) as never)).toBe(false)
    expect(isPopupOpen(root([[0, 'n-popconfirm']]) as never)).toBe(false)
    expect(
      isPopupOpen(root([[1, 'n-base-select-menu fade-in-scale-up-transition-leave-active']]) as never),
    ).toBe(false)
    expect(isPopupOpen(root([[0, 'n-popconfirm'], [1, 'n-dropdown-menu']]) as never)).toBe(true)
  })
})
