# syntax=docker/dockerfile:1.7
#
# Multi-stage build.
#   backend-cpu   : FastAPI + local models on CPU (default)
#   backend-cuda  : same, on an NVIDIA CUDA base image
#   frontend      : Vue SPA served by nginx
#
# ffmpeg is NOT installed from the OS package manager. It arrives as a Python
# dependency: PyAV (`av`) wheels bundle FFmpeg shared libraries, imageio-ffmpeg
# bundles a static CLI as fallback (see docs/research/bundled-ffmpeg.md). The
# image therefore contains exactly the ffmpeg build the application was tested with.
#
# Model weights are not baked in: they live in the /models volume and are fetched explicitly with
# `docker compose run --rm backend asr models download ...` (AGENTS.md rule 6). MODELSCOPE_CACHE /
# HF_HOME only hold the hub libraries' own metadata, kept apart from the <hub>/<repo> model tree.
# Deployment guide: docs/DEPLOYMENT.md.

# ---------------------------------------------------------------- backend base
FROM python:3.12-slim-bookworm AS backend-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# libgomp: OpenMP runtime for torch / onnxruntime CPU kernels
# libsndfile1: soundfile wheels on manylinux bundle it, kept as a safety net
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /bin/

WORKDIR /app/backend

# Dependency layer first for cache efficiency
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --extra cpu --extra local

COPY backend/ ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra cpu --extra local

RUN useradd --create-home --uid 1000 asr \
 && mkdir -p /data/uploads /models \
 && chown -R asr:asr /data /models /app
USER asr

ENV ASR_DATA_DIR=/data \
    ASR_UPLOAD_ROOTS=/data/uploads \
    ASR_MODEL_CACHE_DIR=/models \
    ASR_DEVICE=cpu \
    MODELSCOPE_CACHE=/models/.cache/modelscope \
    HF_HOME=/models/.cache/huggingface

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

# -------------------------------------------------------------- backend (CPU)
FROM backend-base AS backend-cpu
# `asr serve`: one uvicorn worker (the job queue is in-process), ASR_LOG_* honoured. Behind a
# reverse proxy add `--forwarded-allow-ips <proxy address>` (docs/DEPLOYMENT.md).
CMD ["asr", "serve", "--host", "0.0.0.0", "--port", "8000"]

# ------------------------------------------------------------- backend (CUDA)
# Same application, CUDA-enabled torch wheels (pyproject extra "cuda").
FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu22.04 AS backend-cuda

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /bin/
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv python install 3.12 \
 && uv sync --frozen --no-install-project --no-dev --extra cuda --extra local
COPY backend/ ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra cuda --extra local

RUN useradd --create-home --uid 1000 asr \
 && mkdir -p /data/uploads /models \
 && chown -R asr:asr /data /models /app
USER asr
ENV ASR_DATA_DIR=/data \
    ASR_UPLOAD_ROOTS=/data/uploads \
    ASR_MODEL_CACHE_DIR=/models \
    ASR_DEVICE=cuda \
    MODELSCOPE_CACHE=/models/.cache/modelscope \
    HF_HOME=/models/.cache/huggingface
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s \
  CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1
CMD ["asr", "serve", "--host", "0.0.0.0", "--port", "8000"]

# ------------------------------------------------------------- frontend build
FROM node:26-alpine AS frontend-build
# Node >= 25 no longer bundles corepack; pin pnpm to frontend/package.json "packageManager".
RUN npm install -g pnpm@12.14.0
WORKDIR /app/frontend
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

# ------------------------------------------------------------------ frontend
# nginx stable branch (even minor).
FROM nginx:1.30-alpine AS frontend
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=frontend-build /app/frontend/dist /usr/share/nginx/html
# Runtime-configurable backend URL: entrypoint renders /config.js from env.
COPY deploy/frontend-entrypoint.sh /docker-entrypoint.d/40-config.sh
RUN chmod +x /docker-entrypoint.d/40-config.sh
EXPOSE 80
