# Multi-Speaker ASR

Self-hosted speech-to-text with speaker labels and timestamps, tuned for Chinese audio with
embedded English technical terms. Exports txt / srt / vtt / json.

- **Backend** (`backend/`): FastAPI + SQLite, `asr` CLI. Two providers behind one interface:
  `local` (VAD → Qwen3-ASR / Paraformer → forced alignment → CAM++ diarization, CPU or CUDA) and
  `cloud` (Alibaba DashScope filetrans).
- **Frontend** (`frontend/`): Vue 3 SPA — upload, jobs, transcript proofreading, term sets, settings.
- **Desktop** (`desktop/`): Tauri 2 shell around the SPA that records system audio + microphone.

## Quick start (development)

```bash
cd backend && uv sync --extra cpu --extra local
# --no-sync: a plain `uv run` re-syncs without the extras and removes the local model stack
uv run --no-sync asr models download fsmn-vad campplus qwen3-forced-aligner qwen3-asr-0.6b --mirror  # local provider only
uv run --no-sync asr serve            # http://127.0.0.1:8000, API docs at /docs

cd ../frontend && pnpm install && pnpm dev   # http://localhost:5173, proxies /api to :8000
```

One-off transcription without the server: `uv run --no-sync asr transcribe audio.m4a --provider local`;
many files: `uv run --no-sync asr batch <dir> -r --out-dir out/`.

## Deployment

Docker (`docker compose up -d --build`), systemd, Windows, HTTPS, two-host setups, configuration
reference and backups: **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**.

## Documentation

- [PLAN.md](PLAN.md) — architecture, API, milestones
- [AGENTS.md](AGENTS.md) — rules for contributors and coding agents
- [docs/EXECUTION_LOG.md](docs/EXECUTION_LOG.md) — decisions (`Dnn`) and milestone runs
- [docs/ASR_MODEL_COMPARISON.md](docs/ASR_MODEL_COMPARISON.md), [docs/M4_BAKEOFF.md](docs/M4_BAKEOFF.md) — model choice
