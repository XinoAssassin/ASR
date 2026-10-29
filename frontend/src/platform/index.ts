/**
 * Platform boundary. Everything desktop-only (Tauri) goes through here; the web build must never
 * import `@tauri-apps/*` statically. `getDesktop()` loads `./tauri.ts` with a dynamic import only
 * when running inside the Tauri shell, so that code lands in a separate lazy chunk.
 */
import type { DesktopPlatform } from './types'

export * from './types'

/** True inside the Tauri WebView (Tauri 2 injects `window.__TAURI_INTERNALS__`). */
export function isDesktop(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
}

/** Backend the desktop app talks to when nothing else is configured (same-machine setup). */
export const DESKTOP_DEFAULT_BACKEND = 'http://127.0.0.1:8000'

let desktop: Promise<DesktopPlatform> | null = null

/** The desktop facade; rejects in the web build. */
export function getDesktop(): Promise<DesktopPlatform> {
  if (desktop) return desktop
  if (!isDesktop()) return Promise.reject(new Error('desktop platform is not available'))
  desktop = import('./tauri').then((m) => m.createTauriPlatform())
  return desktop
}

/** Test hook: replace (or reset with null) the desktop implementation. */
export function setDesktopPlatformForTests(platform: DesktopPlatform | null): void {
  desktop = platform ? Promise.resolve(platform) : null
}

/** Message of an IPC error (Tauri rejects with the Rust error, usually a string). */
export function ipcErrorMessage(e: unknown): string {
  if (typeof e === 'string') return e
  if (e instanceof Error) return e.message
  if (e && typeof e === 'object' && 'message' in e && typeof e.message === 'string') return e.message
  try {
    return JSON.stringify(e)
  } catch {
    return String(e)
  }
}
