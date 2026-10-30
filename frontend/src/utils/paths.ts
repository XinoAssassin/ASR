/**
 * Server path helpers for the directory picker. Paths come from the backend as-is (POSIX or
 * Windows); this only decides which upload root a typed / remembered path belongs to. The server
 * still validates every target against ASR_UPLOAD_ROOTS (rule 2).
 */

const SEPARATORS = ['/', '\\']

function trimTrailingSeparator(path: string): string {
  let end = path.length
  while (end > 1 && SEPARATORS.includes(path[end - 1]!)) end--
  return path.slice(0, end)
}

/** Whether `path` is `root` or lies below it (`/data/uploads2` is not under `/data/uploads`). */
export function isUnderRoot(path: string, root: string): boolean {
  if (!path || !root) return false
  const r = trimTrailingSeparator(root)
  if (path === r || path === root) return true
  if (!path.startsWith(r)) return false
  // A root such as "/" or "C:\" already ends in a separator.
  if (SEPARATORS.includes(r[r.length - 1]!)) return true
  return SEPARATORS.includes(path[r.length]!)
}

/** The most specific root that contains `path` (nested roots: the deepest one wins). */
export function rootOf<T extends { path: string }>(path: string, roots: T[]): T | undefined {
  let best: T | undefined
  for (const root of roots) {
    if (isUnderRoot(path, root.path) && (!best || root.path.length > best.path.length)) best = root
  }
  return best
}
