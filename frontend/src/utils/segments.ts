/** Segment lookup helpers for the transcript player. */

export interface TimedSegment {
  start: number
  end: number
}

/**
 * Index of the segment playing at `time` in a list sorted by `start`, or -1 in a gap.
 * With overlapping segments the latest-starting one that still covers `time` wins.
 */
export function findActiveSegment(segments: readonly TimedSegment[], time: number): number {
  let lo = 0
  let hi = segments.length - 1
  let last = -1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (segments[mid]!.start <= time) {
      last = mid
      lo = mid + 1
    } else {
      hi = mid - 1
    }
  }
  for (let i = last; i >= 0 && i > last - 8; i--) {
    const seg = segments[i]!
    if (time < seg.end) return i
  }
  return -1
}
