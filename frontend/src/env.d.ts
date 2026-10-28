/// <reference types="vite/client" />

interface AsrRuntimeConfig {
  /** Backend base URL, e.g. "http://gpu-host:8000". Empty = same origin. */
  backendUrl?: string
}

interface Window {
  /** Set by /config.js (public/config.js, or deploy/frontend-entrypoint.sh in Docker). */
  __ASR_CONFIG__?: AsrRuntimeConfig
}
