#!/bin/sh
# Render runtime config for the SPA. The frontend reads window.__ASR_CONFIG__
# at startup; the Settings page can still override the backend URL per browser.
# ASR_BACKEND_URL is the backend base URL as the browser sees it (no trailing /api);
# empty = same origin as the SPA.
set -e
# Escape backslashes and double quotes, drop newlines: the value lands in a JS string literal.
url=$(printf '%s' "${ASR_BACKEND_URL:-}" | tr -d '\r\n' | sed 's/[\\"]/\\&/g')
cat > /usr/share/nginx/html/config.js <<EOF
window.__ASR_CONFIG__ = {
  backendUrl: "${url}"
};
EOF
