#!/bin/sh
# Render runtime config for the SPA. The frontend reads window.__ASR_CONFIG__
# at startup; the Settings page can still override the backend URL per browser.
set -e
cat > /usr/share/nginx/html/config.js <<EOF
window.__ASR_CONFIG__ = {
  backendUrl: "${ASR_BACKEND_URL:-}"
};
EOF
