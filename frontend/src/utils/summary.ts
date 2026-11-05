/**
 * Summary citations (refs → transcript segments). Key points, decisions and action items carry
 * `refs`: up to 3 segment ids in transcript order. A ref can go stale after edits (a merge removes
 * the second segment's id): it is then looked up in `original` (immutable) and mapped to the
 * current segment playing at that time; a ref found nowhere is skipped.
 *
 * The API always returns `{text, refs}` objects, but a summary cached from an older backend may
 * still hold plain strings, so the readers accept both.
 */
import { findActiveSegment, type TimedSegment } from './segments'

export interface SummaryEntry {
  text: string
  refs: string[]
}

export interface SummaryActionEntry {
  task: string
  owner: string | null
  due: string | null
  refs: string[]
}

/** Narrow local view of what may arrive: current objects or pre-citation strings. */
type PointIn = string | { text?: unknown; refs?: unknown } | null | undefined
type ActionIn = { task?: unknown; owner?: unknown; due?: unknown; refs?: unknown } | null | undefined

function refsOf(raw: unknown): string[] {
  return Array.isArray(raw) ? raw.filter((r): r is string => typeof r === 'string' && !!r) : []
}

const str = (v: unknown): string | null => (typeof v === 'string' && v ? v : null)

/** `key_points` / `decisions` in either shape → entries (empty texts dropped). */
export function summaryEntries(items: readonly unknown[] | null | undefined): SummaryEntry[] {
  const out: SummaryEntry[] = []
  for (const raw of (items ?? []) as readonly PointIn[]) {
    if (typeof raw === 'string') {
      if (raw.trim()) out.push({ text: raw, refs: [] })
    } else if (raw && typeof raw === 'object' && typeof raw.text === 'string' && raw.text.trim()) {
      out.push({ text: raw.text, refs: refsOf(raw.refs) })
    }
  }
  return out
}

export function summaryActions(items: readonly unknown[] | null | undefined): SummaryActionEntry[] {
  const out: SummaryActionEntry[] = []
  for (const raw of (items ?? []) as readonly ActionIn[]) {
    if (!raw || typeof raw !== 'object' || typeof raw.task !== 'string' || !raw.task.trim()) continue
    out.push({ task: raw.task, owner: str(raw.owner), due: str(raw.due), refs: refsOf(raw.refs) })
  }
  return out
}

export interface RefSegment extends TimedSegment {
  id: string
  speaker_id: string
  text: string
}

/** Where a citation points now: the segment to jump to and the time to seek. */
export interface ResolvedRef {
  /** The cited id. */
  ref: string
  /** Current segment to highlight / scroll to. */
  segmentId: string
  /** Seconds to seek (the cited segment's start, also when it came from `original`). */
  start: number
  speakerId: string
  text: string
}

/**
 * Resolve citations against the current segments (sorted by start), falling back to `original`.
 * Unresolvable refs are skipped; duplicates (two stale refs landing in one merged segment) are
 * collapsed. Keeps the input (transcript) order.
 */
export function resolveRefs(
  refs: readonly string[],
  segments: readonly RefSegment[],
  original: readonly RefSegment[] = [],
): ResolvedRef[] {
  const out: ResolvedRef[] = []
  const seen = new Set<string>()
  for (const ref of refs) {
    let hit: ResolvedRef | null = null
    const seg = segments.find((s) => s.id === ref)
    if (seg) {
      hit = { ref, segmentId: seg.id, start: seg.start, speakerId: seg.speaker_id, text: seg.text }
    } else {
      const orig = original.find((s) => s.id === ref)
      if (orig) {
        // Stale after a merge: the current segment covering that moment (else the one before it).
        let i = findActiveSegment(segments, orig.start + 0.01)
        if (i < 0) i = lastStartingBefore(segments, orig.start)
        const now = segments[i]
        if (now) {
          hit = { ref, segmentId: now.id, start: orig.start, speakerId: now.speaker_id, text: now.text }
        }
      }
    }
    if (hit && !seen.has(hit.segmentId)) {
      seen.add(hit.segmentId)
      out.push(hit)
    }
  }
  return out
}

function lastStartingBefore(segments: readonly TimedSegment[], time: number): number {
  let index = -1
  for (let i = 0; i < segments.length; i++) {
    if (segments[i]!.start <= time) index = i
    else break
  }
  return index
}
