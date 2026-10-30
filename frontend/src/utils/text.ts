/**
 * Index conversion between JavaScript strings (UTF-16 code units) and the backend, which counts
 * Unicode code points (Python `str`): `SplitOp.at` and `Correction.offset` are code-point
 * indices. They only differ for characters outside the BMP (emoji, rare CJK extension B+).
 */

function isHighSurrogate(code: number): boolean {
  return code >= 0xd800 && code <= 0xdbff
}

function isLowSurrogate(code: number): boolean {
  return code >= 0xdc00 && code <= 0xdfff
}

/** Number of code points in `text` (what Python's `len()` returns). */
export function codePointLength(text: string): number {
  let n = 0
  for (let i = 0; i < text.length; i++) {
    if (isHighSurrogate(text.charCodeAt(i)) && isLowSurrogate(text.charCodeAt(i + 1))) i++
    n++
  }
  return n
}

/**
 * UTF-16 index (e.g. `selectionStart`) → code-point index for the backend. Clamped to
 * [0, length]; an index inside a surrogate pair counts as before that character.
 */
export function utf16ToCodePoint(text: string, index: number): number {
  const end = Math.max(0, Math.min(text.length, Math.trunc(index)))
  let n = 0
  for (let i = 0; i < end; i++) {
    if (isHighSurrogate(text.charCodeAt(i)) && isLowSurrogate(text.charCodeAt(i + 1))) {
      if (i + 1 >= end) break // `end` splits the pair
      i++
    }
    n++
  }
  return n
}

/** Code-point index from the backend → UTF-16 index for `slice()` / highlighting. Clamped. */
export function codePointToUtf16(text: string, index: number): number {
  const target = Math.max(0, Math.trunc(index))
  let i = 0
  for (let n = 0; n < target && i < text.length; n++) {
    if (isHighSurrogate(text.charCodeAt(i)) && isLowSurrogate(text.charCodeAt(i + 1))) i += 2
    else i++
  }
  return i
}
