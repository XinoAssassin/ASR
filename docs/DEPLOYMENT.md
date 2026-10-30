# Deployment guide

How to run the backend and the frontend in production, on one machine or on two (the M8 target:
static frontend on one host, backend — possibly on a GPU server — on another). Architecture and API
are in `PLAN.md`; security decisions referenced as `Dnn` are in `docs/EXECUTION_LOG.md`.

Contents: [1 Topology](#1-topology) · [2 Backend](#2-backend) · [3 Frontend](#3-frontend) ·
[4 Configuration reference](#4-configuration-reference) · [5 Access control](#5-access-control-api-key-cors-hosts) ·
[6 HTTPS and reverse proxy](#6-https-and-reverse-proxy) · [7 Models](#7-models) · [8 CPU, CUDA and ROCm](#8-cpu-cuda-and-rocm) ·
[9 Batch CLI](#9-batch-cli) · [10 Logs](#10-logs) · [11 Backup and upgrade](#11-backup-and-upgrade) ·
[12 Checklist](#12-two-host-checklist) · [13 Verification status](#13-verification-status)

## 1. Topology

```
 Browser ──HTTPS──▶ Static host (nginx / any static server)          serves index.html, assets, config.js
    │
    │  JS fetches, X-API-Key header, SSE
    ▼
 Backend host ──▶ asr serve (FastAPI, one process)                   :8000, often behind nginx :443
    │               ├─ SQLite  ASR_DATA_DIR/asr.db
    │               ├─ uploads ASR_UPLOAD_ROOTS
    │               ├─ local provider: models in ASR_MODEL_CACHE_DIR (CPU, CUDA or ROCm)
    │               └─ cloud provider: DashScope (key stays on this host)
 Desktop app (Tauri, origin http://tauri.localhost) ──▶ same backend API
```

- The SPA is plain static files. It learns the backend URL from `/config.js` at load time; each
  user can override it in Settings (stored in that browser).
- The browser talks to the backend **directly** (cross-origin), so the backend must allow the SPA's
  origin in `ASR_CORS_ORIGINS` and, because it is reachable from the network, require `ASR_API_KEY`.
- Alternative: serve the SPA and proxy `/api/` from **one** origin ([§6.3](#63-single-origin-alternative)).
  Then no CORS is involved and `config.js` keeps `backendUrl: ""`.
- **The backend must run as exactly one process per `ASR_DATA_DIR`.** `asr serve` always starts
  one worker; never run `uvicorn server.app:app --workers N`, gunicorn with several workers, or
  several containers / replicas / hosts against the same data dir (a shared volume included).
  Why: the job queue, the running jobs' cancel flags and live progress, and the SSE subscribers
  are in-memory state of that process (a second process would never see them: cancel and SSE
  break, and on start it marks the other process's running jobs "interrupted by restart"), the
  local models are loaded once per process, and SQLite (`asr.db`, WAL) is a single-host,
  single-writer store. The server enforces this: at startup it takes an advisory lock on
  `ASR_DATA_DIR/server.lock`, and a second server on the same data dir refuses to start with
  "another ASR server is already running on ASR_DATA_DIR ...". The OS releases the lock when the
  process exits, crashes included, so there is no stale lock to delete (on a filesystem without
  lock support the guard only logs a warning). Scale up (bigger GPU), not out;
  a second instance needs its own `ASR_DATA_DIR` (and its own upload roots).

Browser rules that shape the setup:

- An HTTPS page cannot call an `http://` backend (mixed content). If the SPA is on HTTPS, the
  backend must be on HTTPS too ([§6](#6-https-and-reverse-proxy)).
- Recent Chromium-based browsers ask the user for "local network access" when a page from a public
  origin calls a private address (e.g. a public `https://asr.example.com` calling a backend whose
  name resolves to `192.168.x.x`). Keep both on the LAN, or both public.
- The API key and backend-URL override live in the browser's `localStorage`, per SPA origin.

## 2. Backend

Pick one of: Docker ([§2.1](#21-docker)), Linux + systemd ([§2.2](#22-linux-with-systemd)),
Windows ([§2.3](#23-windows)). All of them end up running

```
asr serve --host <addr> --port <port> [--forwarded-allow-ips <proxy-ip>]
```

`asr serve` options: `--host` (default `127.0.0.1`; `0.0.0.0` for LAN, which requires `ASR_API_KEY`), `--port` (default 8000),
`--forwarded-allow-ips` (comma-separated proxy addresses whose `X-Forwarded-For/-Proto` are
trusted; omit when no proxy; requires `ASR_API_KEY`), `--reload` (development only). Shutdown drains for up to 30 s; a job
still running is marked failed ("interrupted") at the next start and can be retried, queued jobs
resume. `GET /api/health` needs no key and is the liveness probe.

### 2.1 Docker

Images (multi-stage `Dockerfile`):

| Target | Base | Contents |
|---|---|---|
| `backend-cpu` | `python:3.12-slim-bookworm` | backend + local model stack, CPU torch |
| `backend-cuda` | `nvidia/cuda:12.6.3-cudnn-runtime-ubuntu22.04` | same, cu126 torch wheels |
| `backend-rocm` | `python:3.12-slim-bookworm` | same, rocm7.2 torch wheel (bundles the ROCm runtime; ~15 GB image) |
| `frontend` | `nginx:1.30-alpine` | built SPA + runtime `config.js` |

No OS ffmpeg is installed; decoding uses the FFmpeg bundled in PyAV wheels, with imageio-ffmpeg
as fallback (AGENTS rule 1). Model weights are not in the image.

```bash
cp .env.example .env            # then edit: ASR_API_KEY, ASR_CORS_ORIGINS, ASR_BIND_ADDR, ...
docker compose up -d --build backend                  # CPU backend
docker compose -f docker-compose.yml -f docker-compose.cuda.yml up -d --build backend   # NVIDIA
docker compose -f docker-compose.yml -f docker-compose.rocm.yml up -d --build backend   # AMD (ROCm)
docker compose up -d --build frontend                 # static host (needs ASR_BACKEND_URL)
docker compose up -d --build                          # both on one host
```

- Ports are published on `${ASR_BIND_ADDR:-127.0.0.1}` (D25). For LAN access set
  `ASR_BIND_ADDR=0.0.0.0` **and** `ASR_API_KEY`. Host ports: `ASR_BACKEND_PORT` (8000),
  `ASR_FRONTEND_PORT` (8080). Compose sets `ASR_ALLOW_UNAUTHENTICATED_NETWORK=1` so the keyless
  default works through the loopback publish ([§5](#5-access-control-api-key-cors-hosts)).
- The application code (`/app`, `/opt/venv`) is root-owned and read-only for the runtime user;
  only `/data` and `/models` are writable.
- Volumes: `asr-data` → `/data` (database, uploads; back it up), `asr-models` → `/models`.
  The container runs as uid 1000; bind mounts must be writable by that uid.
- In the container the paths are fixed (`/data`, `/data/uploads`, `/models`); `ASR_DATA_DIR`,
  `ASR_UPLOAD_ROOTS` and `ASR_MODEL_CACHE_DIR` from `.env` are not passed through. To add an
  upload root (e.g. a NAS share), mount it and extend `ASR_UPLOAD_ROOTS` in the compose file:
  `ASR_UPLOAD_ROOTS: /data/uploads:/mnt/nas`.
- CUDA needs the NVIDIA driver and NVIDIA Container Toolkit on the host;
  `docker-compose.cuda.yml` switches the build target to `backend-cuda`, sets `ASR_DEVICE=cuda` and
  reserves the GPUs.
- ROCm needs only the amdgpu kernel driver on the host (no host ROCm install);
  `docker-compose.rocm.yml` switches to `backend-rocm`, sets `ASR_DEVICE=rocm`, passes `/dev/kfd`
  and `/dev/dri` and adds the `video` group plus the host's `render` gid, which it reads from
  `ASR_RENDER_GID` (`getent group render | cut -d: -f3`; the image has no `render` group of its own).
  See [§8](#8-cpu-cuda-and-rocm) for supported GPUs.
- Behind a reverse proxy, override `command:` with `--forwarded-allow-ips` (commented in
  `docker-compose.yml`).
- Download models into the volume before the first local job ([§7](#7-models)).

### 2.2 Linux with systemd

```bash
sudo useradd --system --create-home --home-dir /var/lib/asr asr
sudo -u asr git clone <repo-url> /opt/asr        # needs /opt/asr owned by asr, or clone then chown
cd /opt/asr/backend
sudo -u asr uv sync --frozen --no-dev --extra cpu --extra local      # NVIDIA: --extra cuda, AMD: --extra rocm
sudo install -d -m 750 -o root -g asr /etc/asr
sudo install -m 640 -o root -g asr /dev/null /etc/asr/asr.env
sudoedit /etc/asr/asr.env      # ASR_API_KEY=..., ASR_CORS_ORIGINS=..., ASR_PROVIDER_DEFAULT=local, ...
sudo cp /opt/asr/deploy/asr.service /etc/systemd/system/asr.service
sudo systemctl daemon-reload && sudo systemctl enable --now asr
journalctl -u asr -f
```

`deploy/asr.service` runs `asr serve --host 127.0.0.1 --port 8000 --forwarded-allow-ips 127.0.0.1`
for use behind nginx on the same host ([§6](#6-https-and-reverse-proxy)); a commented `ExecStart`
binds `0.0.0.0` for direct LAN access. It pins the data/upload/model paths under `/var/lib/asr`
and is sandboxed (`ProtectSystem=strict`, writable `/var/lib/asr` only — add extra upload roots to
`ReadWritePaths`).

`/etc/asr/asr.env` takes plain `KEY=value` lines (any variable from [§4](#4-configuration-reference)):
systemd keeps an inline `# comment` as part of the value, so do not copy `.env.example` verbatim.
Leave the path variables out of it — `EnvironmentFile=` values override the unit's
`Environment=` lines, and a relative `./data` would point into the read-only checkout.

### 2.3 Windows

```powershell
winget install --id=astral-sh.uv -e
cd D:\ASR\app\backend                   # repository checked out to D:\ASR\app
uv sync --frozen --no-dev --extra cpu --extra local
$env:ASR_MODEL_CACHE_DIR = 'D:\ASR\models'
.\.venv\Scripts\asr.exe models download fsmn-vad campplus qwen3-forced-aligner qwen3-asr-0.6b --mirror
Set-Content -Encoding utf8 D:\ASR\api-key.txt '<long random key>'
powershell -ExecutionPolicy Bypass -File ..\deploy\windows\run-backend.ps1 -BindHost 0.0.0.0 -ApiKeyFile D:\ASR\api-key.txt `
  -CorsOrigins 'https://asr-ui.example.com,http://tauri.localhost'
```

`deploy/windows/run-backend.ps1` sets the variables for its own process only (data, uploads,
models under `D:\ASR\` by default; all overridable parameters), reads the key from a file so it
never appears on a command line, and starts `asr serve`. Open the port in Windows Firewall
(`New-NetFirewallRule ... -LocalPort 8000 -Profile Private`) when binding `0.0.0.0`. To run it at
logon, register a scheduled task that starts the script with `powershell.exe -WindowStyle Hidden`.
Upload roots on Windows are `;`-separated (`D:\ASR\uploads;\\nas\audio`).

### 2.4 Windows native backend with an AMD GPU (ROCm)

The main desktop setup: website, desktop app and a local backend on the same Windows PC (e.g. a
Ryzen AI Max / Strix Halo with Radeon 8060S). ROCm runs natively on Windows; WSL is not needed.

```powershell
# AMD Adrenalin driver with ROCm support installed; Python 3.12 (py -3.12) and uv on PATH
cd D:\ASR\app\backend
uv sync --frozen --no-dev --extra rocm --extra local     # torch 2.14.0+rocm10.1.0 from AMD's index, ~5 GB venv
.\.venv\Scripts\python.exe -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
$env:ASR_MODEL_CACHE_DIR = 'D:\ASR\models'
.\.venv\Scripts\asr.exe models download fsmn-vad campplus qwen3-forced-aligner qwen3-asr-1.7b --mirror
powershell -ExecutionPolicy Bypass -File ..\deploy\windows\run-backend.ps1 -Device rocm
```

- The `rocm` extra is platform-specific (§8.1): on Windows it installs AMD's torch build for
  gfx1151 only; other AMD GPUs need the target changed in `pyproject.toml` first.
- Desktop app sidecar: set its `command` to `D:\ASR\app\backend\.venv\Scripts\asr.exe`, `args`
  `serve --port 8000`, and put `ASR_DEVICE=rocm` (and `ASR_MODEL_CACHE_DIR`) into the app's
  environment or `backend\.env`; or leave the device on `cpu` and pick ROCm per job / in Settings.
  The Settings page lists what the backend can use (`GET /api/system/hardware`).
- `asr transcribe file.m4a --provider local --device rocm` works the same from the CLI.
- Measured 2026-10-30 on a Ryzen AI MAX+ 392 / Radeon 8060S (gfx1151, 64 GB, Adrenalin driver),
  60 s of the 4-speaker test clip, full pipeline (VAD, ASR, alignment, CAM++ diarization), second
  run (the first is ~10 % slower):

  | Device | Model | RTF | Load | GPU memory |
  |---|---|---|---|---|
  | rocm (Windows) | Qwen3-ASR-1.7B bf16 | 0.17–0.18 | 20 s | 6.0 GB |
  | rocm (Windows) | Qwen3-ASR-0.6B bf16 | 0.13 | 18 s | 3.7 GB |
  | cpu (Windows, same venv, 16 threads) | Qwen3-ASR-1.7B bf16 | 0.55 | 11 s | – |
  | cpu (WSL2, CPU torch 2.14.1, 16 threads) | Qwen3-ASR-1.7B bf16 | 0.44 | 6 s | – |

  The CPU and ROCm transcripts match (same 25 segments and 4 speakers, 98 % identical text).
- AMD's Windows build has two broken kernel libraries on gfx1151, and the backend works around
  both when it loads the models (one warning in the log): the fused flash / memory-efficient
  attention kernels have no code object for the GPU ("device kernel image is invalid"), so
  attention uses torch's math kernel; MIOpen cannot compile its batch-norm kernels at run time
  ("'type_traits' file not found"; MIOpen prints this once into the log), so convolution and
  batch norm use torch's native HIP kernels.

### 2.1 Prebuilt images (GitHub Container Registry)

`.github/workflows/docker.yml` builds the images on every push to `main` (and on `v*` tags) and
publishes them to `ghcr.io/xinoassassin/` (linux/amd64):

| Image | Tags | Use |
|---|---|---|
| `asr-backend` | `cpu`, `sha-<commit>-cpu`, `<version>-cpu` | API + local models on CPU |
| `asr-backend` | `cuda`, `sha-<commit>-cuda`, `<version>-cuda` | API on an NVIDIA GPU (driver + NVIDIA Container Toolkit on the host) |
| `asr-backend` | `rocm`, `sha-<commit>-rocm`, `<version>-rocm` | API on an AMD GPU (amdgpu kernel driver on the host; `devices:` / `group_add:` block in the compose file, [§8](#8-cpu-cuda-and-rocm)) |
| `asr-frontend` | `latest`, `sha-<commit>`, `<version>` | SPA on nginx |

On a NAS (or any Docker host) use `deploy/ghcr/docker-compose.yml`: put it in a folder with a `.env`
holding at least `NAS_HOST` (how browsers reach the NAS) and `ASR_API_KEY` (mandatory, because the
backend port is published on the LAN), then `docker compose pull && docker compose up -d`, download
the models once with `docker compose run --rm backend asr models download … --mirror` (§7) and open
`http://<NAS_HOST>:8080`. Update with `docker compose pull && docker compose up -d`; pin a build
with `ASR_BACKEND_TAG=sha-<commit>-cpu`. If a pull asks for credentials, the package is still
private: make it public in the package settings on GitHub, or `docker login ghcr.io` with a token
that has `read:packages`.

## 3. Frontend

The production build is static: `cd frontend && pnpm install --frozen-lockfile && pnpm build`
→ `frontend/dist/`. Serve it from any web server that

1. falls back to `index.html` for unknown paths (history-mode routes such as
   `/transcripts/<id>`; without it a page reload on those URLs gives 404),
2. serves `/config.js` with `Cache-Control: no-store`,
3. may cache `/assets/*` forever (hashed names),
4. sends `X-Frame-Options: DENY`, `Content-Security-Policy: frame-ancestors 'none'` and
   `X-Content-Type-Options: nosniff` (no framing / clickjacking). In nginx, repeat them in every
   `location` that has its own `add_header` — such a block drops the server-level ones.

`deploy/nginx.conf` does exactly that (it is the frontend image's config). The runtime config:

```js
// /config.js  (source: frontend/public/config.js)
window.__ASR_CONFIG__ = {
  backendUrl: "https://asr-api.example.com"   // as the browser sees it; no trailing /api; "" = same origin
};
```

- Docker: set `ASR_BACKEND_URL`; `deploy/frontend-entrypoint.sh` rewrites `config.js` at every
  container start.
- Plain static host: edit `dist/config.js` after each build/copy — a new build overwrites it with
  the empty default.
- Users can still override the URL per browser in Settings → Connection (also the place to enter
  the API key). Settings → "test" probes `/api/health`.

## 4. Configuration reference

The backend reads, in increasing priority: `<repo>/.env`, `<repo>/backend/.env`, process
environment. Relative paths resolve against the working directory. Exceptions are marked
*env only*: those are read from the process environment directly, so a value in a `.env` file has
no effect (with Docker the compose file passes them as real variables, so `.env` works there).
CLI commands other than `asr serve` (`asr models`, `asr batch`, `asr transcribe`) read the process
environment only — export `ASR_MODEL_CACHE_DIR` (and `DASHSCOPE_API_KEY` for the cloud provider)
in the shell that runs them.

| Variable | Default | Meaning |
|---|---|---|
| `ASR_API_KEY` | empty | Shared `X-API-Key`. Empty disables auth (loopback-only mode, [§5](#5-access-control-api-key-cors-hosts)). |
| `ASR_ALLOWED_HOSTS` | empty | Extra `Host` names accepted while auth is disabled, comma-separated; `*` accepts any (don't). |
| `ASR_ALLOW_UNAUTHENTICATED_NETWORK` | empty (off) | With `ASR_API_KEY` empty: accept clients from any address and let `asr serve` bind a non-loopback `--host`. Only safe when the port is published on loopback alone (compose sets it for its `127.0.0.1:8000:8000` publish). [§5](#5-access-control-api-key-cors-hosts). |
| `ASR_CORS_ORIGINS` | `http://localhost:5173,http://localhost:8080` | Browser origins allowed to call the API, comma-separated, `scheme://host[:port]`; `*` is rejected at startup. |
| `ASR_DATA_DIR` | `./data` | `asr.db`, chunked-upload sessions, caches, job work dirs. |
| `ASR_UPLOAD_ROOTS` | `<data>/uploads` | Directories uploads may be written to / registered from, `os.pathsep`-separated (`:` Linux, `;` Windows). Must not contain `ASR_DATA_DIR` (startup error). |
| `ASR_MODEL_CACHE_DIR` | `./models` | Local model weights, layout `<hub>/<repo>`. |
| `ASR_PROVIDER_DEFAULT` | `cloud` | Default provider for new jobs (`cloud`/`local`); Settings can override. |
| `ASR_DEVICE` | `cpu` | Local inference device (`cpu`/`cuda`/`rocm`); Settings can override. `GET /api/system/hardware` lists what this server can actually use. |
| `ASR_DECODER` | `pyav` | `pyav` or `imageio` (bundled ffmpeg either way). |
| `ASR_TORCH_THREADS` | min(16, CPUs) | *env only.* CPU inference threads. |
| `ASR_LOCAL_DTYPE` | `auto` | *env only.* `auto`/`float32`/`bfloat16`/`float16` ([§8](#8-cpu-cuda-and-rocm)). |
| `ASR_LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR`. |
| `ASR_LOG_FORMAT` | `text` | `text` or `json` (one object per line). |
| `ASR_LOG_FILE` | empty | Optional log file, rotated at 10 MB, 5 backups; stdout/stderr logging continues. |
| `DASHSCOPE_API_KEY` | empty | Cloud provider key; Settings can store one in the database instead. Never sent to browsers. |
| `DASHSCOPE_ENDPOINT` | `https://dashscope.aliyuncs.com` | DashScope endpoint; Settings can override. |
| `ASR_CLOUD_MODEL` | `qwen-audio-3.1-asr-flash-filetrans` | Cloud ASR model; Settings can override. |
| `ASR_CLOUD_FILE_TRANSFER` | `temp_upload` | `temp_upload` or `public_url`. |
| `ASR_LLM_MODEL` | `qwen-plus` | DashScope chat model for the optional LLM correction suggestions. |
| `MODELSCOPE_CACHE`, `HF_HOME` | library defaults | Hub-library metadata caches; the images point them at `/models/.cache/...`. |
| `HTTPS_PROXY`, `NO_PROXY` | — | Honoured by model downloads and cloud calls. |

Docker-only (`docker-compose.yml`): `ASR_BIND_ADDR` (`127.0.0.1`), `ASR_BACKEND_PORT` (8000),
`ASR_FRONTEND_PORT` (8080), `ASR_BACKEND_URL` (rendered into `config.js`).

Settings saved from the UI (default provider, cloud endpoint/model/key/file transfer, local ASR
model/device) are stored in `asr.db` and override the env defaults; `ASR_API_KEY`, CORS, hosts and
paths cannot be changed from the UI by design.

## 5. Access control (API key, CORS, hosts)

**Networked backend (anything not on loopback): set `ASR_API_KEY`.** Generate a long random value,
e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`, and give it to users out of
band; they enter it once per browser in Settings → Connection, and once in the desktop app.

- Every route except `/api/health`, `/openapi.json`, `/docs`, `/redoc` requires the header
  `X-API-Key`; missing or wrong → `401`. The key is accepted from the header only, never from a
  query string.
- **Open mode** (key empty) is for one machine. The backend then accepts only clients connecting
  **from this machine** (the TCP peer must be a loopback address — a `Host: localhost` header
  alone proves nothing; a Unix-socket peer does not count), only
  `Host: localhost / 127.0.0.1 / [::1]` (plus `ASR_ALLOWED_HOSTS`), no request carrying proxy
  headers (`Forwarded`, `X-Forwarded-For`, `X-Real-IP`, `Via` → `403`), and refuses
  state-changing requests whose `Origin` is neither loopback nor in `ASR_CORS_ORIGINS` (D24); the
  app logs a warning. `asr serve --host 0.0.0.0` (or any non-loopback address) and
  `asr serve --forwarded-allow-ips …` **refuse to start** without a key.
- What open mode **cannot** detect: a program on the same host that relays other machines'
  requests over a fresh loopback connection without proxy headers — `nginx` with a bare
  `proxy_pass http://127.0.0.1:8000` (its default upstream `Host` is `127.0.0.1:8000`, which
  passes), `netsh interface portproxy` (the usual Windows/WSL LAN recipe), `socat`, `ssh -R`,
  `pnpm dev --host`. To the backend those requests look exactly like local ones, so every client
  that reaches the forwarder controls the API. **Any proxy or port forwarder in front of the
  backend requires `ASR_API_KEY`.**
- `ASR_ALLOW_UNAUTHENTICATED_NETWORK=1` lifts the peer check and the bind refusal. It exists for
  Docker, where every request arrives from the bridge gateway rather than 127.0.0.1:
  `docker-compose.yml` sets it because it publishes the port on `127.0.0.1` only. It is **only safe
  behind such a loopback-only publish**: with `ASR_BIND_ADDR=0.0.0.0` set `ASR_API_KEY` (and
  optionally `ASR_ALLOW_UNAUTHENTICATED_NETWORK=0`). The image on its own (`docker run -p ...`
  without a key) refuses to start.
- JSON request bodies are capped at **2 MB** (`413`), the upload `init` / `register` calls
  included; the two routes that carry file bytes keep their own limits (`POST /api/uploads`:
  2 GiB per file; `PUT /api/uploads/{id}/parts/{n}`: exact 8 MiB parts).
- `ASR_CORS_ORIGINS` must list **every origin that serves the SPA**, exactly as the browser shows it
  (scheme, host, port; no trailing path): e.g.
  `https://asr-ui.example.com,http://192.168.1.10:8080`. Preflights from other origins get `400`
  and browsers block the call. CORS is not authentication; the key is.
- **Desktop app**: Tauri 2 on Windows serves the SPA from `http://tauri.localhost`; add it to
  `ASR_CORS_ORIGINS` (`docs/desktop-ipc.md`). The app's own uploads (Rust) are not browser requests
  and need the API key but no CORS. A remote backend always needs a key.
- The DashScope key never leaves the backend: `GET /api/settings` returns it masked.
- One shared key, no per-user isolation (PLAN §11 open question 2). Rotate it by changing
  `ASR_API_KEY` and restarting; every browser must re-enter it.

## 6. HTTPS and reverse proxy

`asr serve` speaks plain HTTP. Terminate TLS in a reverse proxy.

### 6.1 Backend behind nginx

`deploy/nginx-backend-https.conf` is a complete example:

- `proxy_buffering off`, `gzip off` and `proxy_read_timeout 1h` on `/api/jobs/<id>/events`
  (server-sent events; the backend pings every 15 s);
- `client_max_body_size 2100m` and `proxy_request_buffering off` (the UI uploads 8 MiB chunks,
  single-request uploads may be up to 2 GiB);
- `proxy_read_timeout 600s` for long requests (big upload parts, LLM summaries);
- `X-Forwarded-For / -Proto / -Host` set, paired with
  `asr serve --host 127.0.0.1 --forwarded-allow-ips 127.0.0.1` so the access log shows client IPs.

A proxy needs `ASR_API_KEY`: `asr serve --forwarded-allow-ips …` refuses to start without one, and
open mode answers `403` to any request carrying `X-Forwarded-For` / `Forwarded` / `X-Real-IP` /
`Via`. Do not work around it with `ASR_ALLOWED_HOSTS` plus `ASR_ALLOW_UNAUTHENTICATED_NETWORK`,
and do not strip the forwarding headers: a proxy that sends none is still a proxy
([§5](#5-access-control-api-key-cors-hosts)).

Other proxies (Caddy, Traefik, IIS ARR) need the same three things: no response buffering for SSE,
large request bodies, long read timeouts.

### 6.2 Frontend

Serve the static files over HTTPS from any host (nginx with `deploy/nginx.conf` plus a TLS server
block, a CDN, object storage with an index fallback). Then `backendUrl` must be an `https://` URL.

### 6.3 Single-origin alternative

One nginx serves the SPA and proxies the API, so the browser sees one origin: copy the two
`location` blocks of `deploy/nginx-backend-https.conf` (with `location /` renamed to
`location /api/`) and its body-size settings into the server block that serves `dist/`, and keep `backendUrl: ""` in `config.js`. CORS is unused
(leave `ASR_CORS_ORIGINS` at its default, add `http://tauri.localhost` for the desktop app).

## 7. Models

Models are **never downloaded implicitly** (AGENTS rule 6): a local job whose models are missing is
refused with `422` and the exact command to run. Two explicit ways:

- **CLI** on the backend host:
  ```bash
  export ASR_MODEL_CACHE_DIR=/var/lib/asr/models     # same dir the server uses
  asr models list
  asr models download fsmn-vad campplus qwen3-forced-aligner qwen3-asr-0.6b --mirror
  ```
  Docker: `docker compose run --rm backend asr models download ... --mirror` (add
  `-f docker-compose.cuda.yml` on GPU hosts so the same volume is used; add
  `-e HTTPS_PROXY=...` if the host needs a proxy).
- **Settings → local models** in the UI (one download at a time, optional ModelScope mirror).

`--mirror` fetches Hugging Face models from their ModelScope mirror — faster and more reliable from
mainland China; ModelScope models ignore it.

| Key | Needed for | Size on disk |
|---|---|---|
| `fsmn-vad` | every local job | 4 MB |
| `campplus` | every local job (diarization) | 28 MB |
| `qwen3-forced-aligner` | Qwen3-ASR and Fun-ASR-Nano jobs | 1.8 GB |
| `qwen3-asr-0.6b` | default on CPUs without native BF16 | 1.5 GB |
| `qwen3-asr-1.7b` | default where bf16 is native (most CUDA GPUs, AVX512_BF16/AMX CPUs) | 3.9 GB |
| `seaco-paraformer` | fast draft mode (own timestamps, no aligner) | 0.95 GB |
| `fun-asr-nano` | optional | 1.6 GB |

`asr models list` prints the full, current registry (optional models such as the faster-whisper
English backend may be listed beyond this table).

The set a job needs is `fsmn-vad` + the ASR model + `qwen3-forced-aligner` (except Paraformer) +
`campplus`. A cloud-only deployment needs no models (the image still contains the local stack).
Copying a populated `ASR_MODEL_CACHE_DIR` to an offline host works: "downloaded" means the weights
file is present and no partial files remain.

## 8. CPU, CUDA and ROCm

`GET /api/system/hardware` reports, for `cpu`, `cuda` and `rocm`, whether this server can use it
(with the CPU / GPU name, or why not, e.g. "torch 2.14.1+cpu is a CPU build"); `default` is the
usable GPU, else `cpu`. It is probed once per server process (torch import, no downloads), and a
local job whose device is unavailable is rejected with 422 and that reason.

- `ASR_DEVICE=cpu` works everywhere. Default ASR model: Qwen3-ASR-1.7B in bfloat16 on CPUs with
  native BF16 (AVX512_BF16 / AMX; RTF ≈ 0.43 in the M4 bake-off), otherwise Qwen3-ASR-0.6B in
  float32. `ASR_TORCH_THREADS` caps the threads (default min(16, CPUs)); leave cores for the web
  process on small hosts.
- `ASR_DEVICE=cuda` (CUDA image or `uv sync --extra cuda`): bfloat16 and Qwen3-ASR-1.7B when the
  GPU supports bf16 (Ampere and newer), otherwise float16 and Qwen3-ASR-0.6B.
- `ASR_DEVICE=rocm` (ROCm image, or `uv sync --extra rocm --extra local`): AMD GPUs. ROCm builds of
  torch drive HIP GPUs through torch's `cuda` device, so `rocm` runs there after checking that the
  installed torch is a ROCm build (`torch.version.hip`); a clear error names the fix otherwise.
  torch reports bf16 on every HIP GPU, so the default is bfloat16 and Qwen3-ASR-1.7B; on RDNA2
  (gfx1030), which has no bf16 matrix units, try `ASR_LOCAL_DTYPE=float16`. faster-whisper
  (CTranslate2) has no ROCm build: on `rocm` the Whisper model runs on the CPU (VAD and
  diarization stay on the GPU) and the job carries a warning.
- SeACo-Paraformer always runs in float32 (its CIF predictor breaks in bf16; D31). Force a dtype
  with `ASR_LOCAL_DTYPE` only for experiments.
- Local jobs run one at a time; cloud jobs run concurrently.
- The CUDA path has not yet been verified on an NVIDIA host (PLAN M6).

### 8.1 ROCm builds

| Platform | torch build | Index (pyproject `[tool.uv.index]`) | GPU kernels |
|---|---|---|---|
| Linux (image `backend-rocm`, `uv sync --extra rocm`) | `2.14.1+rocm7.2` | `https://download.pytorch.org/whl/rocm7.2` (`pytorch-rocm`) | one 6 GB wheel with the ROCm 7.2.1 libraries; torch built for gfx900/906/908/90a/942/950, gfx1030, gfx1100–1103, gfx1150/1151, gfx1200/1201 |
| Windows (native, `uv sync --extra rocm`) | `2.14.0+rocm10.1.0` | `https://stable.repo.amd.com/rocm/whl-next/` (`amd-rocm-windows`) | AMD's TheRock wheels: torch + `rocm-sdk-*` (~1.2 GB), kernels for gfx1151 only (`amd-torch-device-gfx1151`, `rocm-sdk-device-gfx1151`) |

- download.pytorch.org has no Windows ROCm wheels; AMD publishes them, and for Windows the newest
  torch is 2.14.0 (not 2.14.1, the pin of every other flavour). The Windows wheel set is tied to
  one GPU target: for another AMD GPU replace `gfx1151` in the `amd-torch-device-gfx1151` and
  `rocm-sdk-device-gfx1151` entries of `backend/pyproject.toml` (extra and sources) with its
  target, e.g. `gfx1100` for RX 7900, `gfx1201` for RX 9070, and run `uv lock`.
- PyTorch 2.14 also publishes `rocm7.14` wheels (TheRock packaging: torch 1.4 GB plus
  `rocm[device-all]`, which pulls every GPU family's kernels, several GB more). The Linux image
  stays on the self-contained `rocm7.2` wheel.
- Supported GPUs (Linux, ROCm 7.2 matrix): Instinct MI100/MI200/MI300/MI350 (gfx908/90a/942/950),
  Radeon PRO W6800/V620 (gfx1030), RX 7900/7800/7700 and PRO W7900/W7800/W7700 (gfx1100/1101),
  RX 9070/9060 and AI PRO R9700 (gfx1200/1201). ROCm 7.14 / 10.x add RX 7600 (gfx1102) and the
  Ryzen APUs: Ryzen AI Max (Strix Halo, Radeon 8060S/8050S, gfx1151), Ryzen AI 300 (Radeon 890M/880M,
  gfx1150), Radeon 780M/760M (gfx1103)
  ([7.2 matrix](https://rocm.docs.amd.com/en/docs-7.2.0/compatibility/compatibility-matrix.html),
  [7.14 matrix](https://rocm.docs.amd.com/en/docs-7.14.0/compatibility/compatibility-matrix.html)).
  The `rocm7.2` wheel still ships rocBLAS / hipBLASLt kernels for gfx1150/1151 (checked in its file
  list), and AMD lists ROCm 7.2.0 on Strix Halo as supported on Ubuntu 24.04 OEM, Ubuntu 26.04 and
  any distribution with kernel ≥ 6.18.4; older kernels lack the KFD fixes and compute may fail to
  start ([Strix Halo guide](https://rocm.docs.amd.com/en/docs-7.2.0/how-to/system-optimization/strixhalo.html)).
  On Strix Halo keep the BIOS VRAM carve-out small and raise the GTT limit instead
  (`amd-ttm --set <GB>`, same guide): PyTorch allocates from GTT.
- GPUs outside the list: `HSA_OVERRIDE_GFX_VERSION` makes the runtime treat the GPU as the nearest
  supported ISA (Linux only), e.g. `10.3.0` for RX 6700 XT / 6600 (gfx1031/1032), `11.0.0` for
  the 780M (gfx1103). Unsupported by AMD; wrong values crash or compute garbage.
- Kernel probe: when local models load on a ROCm GPU, the backend runs flash attention,
  memory-efficient attention and MIOpen batch norm / conv once on tiny inputs and switches off
  whatever fails (found on Windows, §2.4), so a missing GPU kernel degrades speed instead of
  failing every job.
- Docker: pass `/dev/kfd` and `/dev/dri` (or single `/dev/dri/renderD*` nodes), add `video` and
  the host's numeric `render` gid (`ASR_RENDER_GID`); `--security-opt seccomp=unconfined` is
  optional per AMD's container guide. MIOpen keeps its compiled-kernel databases in
  `/models/.cache/miopen` so restarts do not recompile them.
- WSL2: ROCm inside WSL is a technical preview and needs AMD's WSL driver stack; this project's
  development WSL has no `/dev/kfd`, so the AMD GPU is used from the Windows side (§2.4).
- Strix Halo: running the NPU (FastFlowLM / Lemonade) and ROCm on the iGPU at the same time causes
  NPU timeouts and, in one report, a machine-check hang
  ([xdna-driver #1605](https://github.com/amd/xdna-driver/issues/1605), open as of 2026-10);
  see `docs/research/amd-npu.md`.

## 9. Batch CLI

Transcribe many files with one model load, without the server:

```bash
export ASR_MODEL_CACHE_DIR=/var/lib/asr/models
asr batch /srv/audio/meetings -r --out-dir /srv/audio/out -f txt -f srt -f json
asr batch a.m4a b.wav --asr-model seaco-paraformer --device cpu --terms "CUDA" --terms-file terms.txt
```

- Inputs: files and/or directories (`-r` recurses). Outputs `<stem>.<fmt>` next to each input or in
  `--out-dir`; default formats `txt` and `json` (`srt` and `vtt` also available).
- Files whose outputs all exist are skipped (`--overwrite` to redo); failures are listed at the end
  and make the exit code 1.
- Default provider is `local`; `--provider cloud` needs `DASHSCOPE_API_KEY` in the environment and
  costs money.
- Same post-correction ([5b] L1 term correction + repetition filter) as server jobs.
- Docker: `docker compose run --rm -v /srv/audio:/audio backend asr batch /audio -r --out-dir /audio/out`
  (the directory must be writable by uid 1000).
- funasr prints "ffmpeg is not installed ... apt install ffmpeg" while loading; ignore it — decoding
  goes through the bundled FFmpeg.

## 10. Logs

- `asr serve` logs to stderr (captured by journald / `docker logs`), and additionally to
  `ASR_LOG_FILE` if set (rotated at 10 MB, 5 backups).
- `ASR_LOG_FORMAT=json` writes one JSON object per line: `ts`, `level`, `logger`, `msg`, plus
  structured fields. Job lines carry them, e.g.
  ```json
  {"ts": "...", "level": "INFO", "logger": "server.jobs", "msg": "job 52a1... succeeded: 3 segments, 1 speakers, 30.0 s audio in 16.9 s", "job_id": "52a1...", "status": "succeeded", "elapsed_s": 16.9, "audio_s": 30.0}
  ```
  Filter one job: `jq 'select(.job_id=="<id>")' asr.log`.
- uvicorn access lines (`uvicorn.access`) show the client address — the real one only with
  `--forwarded-allow-ips` behind a proxy.
- Every handler redacts URL query strings (signed result URLs), bearer tokens and `sk-` keys
  (D47). The `X-API-Key` header is never logged. This also holds for a plain
  `uvicorn server.app:app` run: the app installs the same handlers when none are configured and
  adds the redaction filter to uvicorn's own handlers.

## 11. Backup and upgrade

**Back up** `ASR_DATA_DIR/asr.db` and every `ASR_UPLOAD_ROOTS` directory (the audio). `cache/`,
`work/` and `upload-sessions/` under the data dir are disposable; models can be re-downloaded.
The database runs in WAL mode, so copy it with SQLite's online backup, not `cp`:

```bash
python3 -c "import sqlite3; s=sqlite3.connect('/var/lib/asr/data/asr.db'); d=sqlite3.connect('/backup/asr.db'); s.backup(d); d.close()"
# Docker:
docker compose exec backend python -c "import sqlite3; s=sqlite3.connect('/data/asr.db'); d=sqlite3.connect('/data/asr-backup.db'); s.backup(d); d.close()"
docker compose cp backend:/data/asr-backup.db ./asr-backup.db
```

(or stop the service and copy `asr.db` together with `asr.db-wal`). Restore = stop, put `asr.db`
back (delete stale `-wal`/`-shm`), start. Uploads are referenced by absolute path, so restore them
to the same location.

**Upgrade**:

```bash
# back up first, then
git pull
cd backend && uv sync --frozen --no-dev --extra cpu --extra local   # same extras as before
sudo systemctl restart asr        # schema migrations run automatically at startup
cd ../frontend && pnpm install --frozen-lockfile && pnpm build      # then redeploy dist/, re-apply config.js
# Docker:
docker compose build --pull && docker compose up -d
```

Migrations only move forward; to roll back, restore the database backup together with the old
code. Read `docs/EXECUTION_LOG.md` for decisions that change configuration.

## 12. Two-host checklist

Backend host (`asr-api.example.com`, GPU optional):

1. Install ([§2](#2-backend)), download models ([§7](#7-models)).
2. `ASR_API_KEY=<random>`, `ASR_CORS_ORIGINS=https://asr-ui.example.com[,http://tauri.localhost]`.
3. HTTPS proxy ([§6](#6-https-and-reverse-proxy)) or, on a trusted LAN, `asr serve --host 0.0.0.0`
   plus a firewall rule.
4. `curl https://asr-api.example.com/api/health` → `"api_key_required": true`;
   `curl https://asr-api.example.com/api/jobs` → 401.

Frontend host (`asr-ui.example.com`):

5. Deploy `dist/` (or the `frontend` image) with `config.js` → `backendUrl: "https://asr-api.example.com"`.
6. Open the SPA: the health indicator turns green (backend reachable, CORS allowed; `/api/health`
   needs no key). Settings → Connection → enter the API key; the Jobs page and Settings now load
   instead of reporting a missing key.
7. Upload a short file, start a job, open the transcript, export.

## 13. Verification status

Verified 2026-10-29 (M8 acceptance, simulated two hosts on one PC):

- Backend: `asr serve --host 0.0.0.0 --port 8060` in WSL2 with `ASR_API_KEY` set,
  `ASR_LOG_FORMAT=json`, `ASR_LOG_FILE`, reached only through the machine's LAN address.
- Frontend: production build with `config.js` pointing at the LAN URL, served by a separate Windows
  process (`python -m http.server`), driven by headless Chromium: API key entered in Settings,
  local ASR model chosen in Settings, 30 s clip uploaded in chunks, local job (SeACo-Paraformer,
  CPU) followed over SSE in the Jobs page, transcript opened, TXT exported. The job itself was
  created with a `fetch` from the SPA's page (same origin, key and CORS path as the UI) because the
  Upload page's start button only enables for the cloud provider at the time of the run.
- No key / wrong key → 401; preflight from an unlisted origin → 400 without
  `Access-Control-Allow-Origin`; auth-off mode via the LAN address → 403 (Host), cross-site POST →
  403; `--forwarded-allow-ips` puts the `X-Forwarded-For` address in the access log; JSON log lines
  carry `job_id`; `asr batch` on two clips wrote txt/srt/json and skipped them on the rerun.

ROCm, 2026-10-30: the Windows native backend (`uv sync --extra rocm --extra local`, torch
2.14.0+rocm10.1.0) ran on the Radeon 8060S: `GET /api/system/hardware` reported rocm, and the
full local pipeline ran on the GPU ([§2.4](#24-windows-native-backend-with-an-amd-gpu-rocm) has the
numbers). The Linux `rocm` extra and the `backend-rocm` image were only resolved (`uv lock`) and
checked for the gfx1151 kernels in the wheel's file list: no Linux host with `/dev/kfd` was
available, so the image was neither built nor run.

Not verified: building and running the Docker images (no Docker daemon on the development machine;
the compose files were validated against the compose-spec schema only), the nginx examples (no
nginx available), the systemd unit beyond `systemd-analyze verify`, the CUDA image, and real
two-machine networking (both "hosts" shared one PC and its LAN address).
