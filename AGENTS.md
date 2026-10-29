# AGENTS.md — Multi-Speaker ASR

Guidance for AI coding agents working in this repository. Read this file first, then `PLAN.md`.

## Project summary

Self-hosted multi-speaker speech-to-text tool. Chinese-primary audio with embedded English technical terms.
Output: transcript with speaker labels and timestamps (txt / srt / vtt / json / docx).

Three deliverables sharing one core library:

| Component | Path | Stack |
|---|---|---|
| Backend API | `backend/` | Python 3.12, FastAPI, SQLite, uv |
| Frontend SPA | `frontend/` | Vue 3, Vite, TypeScript, Naive UI, wavesurfer.js, pnpm |
| CLI | `backend/asr/cli.py` | typer |
| Desktop app | `desktop/` | Tauri 2 (Rust) wrapping `frontend/`; records system audio + mic, uploads via the same API |

Two transcription providers behind one `TranscriptionProvider` interface:
- `local`: VAD → ASR (Qwen3-ASR default) → forced alignment → speaker diarization (CAM++) → merge
- `cloud`: Alibaba DashScope `qwen-audio-3.1-asr-flash-filetrans` (async task API; audio must be given as a URL)

Both return the same `Transcript` structure. The frontend never branches on provider.

## Documents

- `PLAN.md` — architecture, API contract, data model, milestones, risks. Keep it current when scope changes.
- `docs/ASR_MODEL_COMPARISON.md` — why Qwen3-ASR / Fun-ASR / FireRedASR over Whisper.
- `docs/research/` — research reports (preprocessing, bundled ffmpeg, cloud file transfer). Read before touching the related module.

All documentation and code comments are written in **English**. User-facing UI strings are Chinese first (i18n keys in `frontend/src/locales/`).

## Hard rules

1. **No host ffmpeg.** Decoding/probing goes through PyAV (`av`, bundled FFmpeg libs) with imageio-ffmpeg as the subprocess fallback — see `docs/research/bundled-ffmpeg.md`. Never call a bare `ffmpeg` on `PATH`.
1a. **Preprocessing is decode-only by default.** No denoise / loudnorm / separation unless the job's `PreprocessOptions` turns it on (`docs/research/audio-preprocessing.md`).
2. **Upload path safety.** Any user-supplied `target_dir` must resolve (realpath) inside one of `ASR_UPLOAD_ROOTS`. Reject `..`, absolute paths outside roots, and symlink escapes. Covered by tests in `backend/tests/test_storage.py`.
3. **Secrets stay server-side.** DashScope API keys are read from env/settings on the backend and returned masked by `GET /api/settings`. Never send them to the browser or log them.
4. **Provider contract.** New providers implement `asr/providers/base.py` and must pass `backend/tests/test_provider_contract.py`. Do not leak provider-specific fields into `Transcript`.
5. **Core library is Web-free.** `backend/asr/` must not import FastAPI or anything from `backend/server/`.
6. **Models are local by default.** Model weights load from `ASR_MODEL_CACHE_DIR`; downloads happen only via the explicit settings endpoint or CLI command, never implicitly during a request.
7. **Python 3.12, pinned via `.python-version` and `uv.lock`.** Do not bump without checking `qwen-asr`, `funasr`, `torch` compatibility.
8. **Desktop recorder never loses audio.** The local recording file is written incrementally and kept until the backend job succeeds; upload failures go to a retry queue, never to deletion.
9. **Edit transcripts non-destructively.** User edits go to `segments`; `original` is immutable so `POST /api/transcripts/{id}/revert` always works.

## Environment notes

- Dev machine: Windows 11, AMD CPU (no CUDA), 64 GB RAM. Test locally with `device=cpu`; CUDA paths are verified on a separate NVIDIA machine or in the CUDA Docker image.
- Use `uv` for Python (`uv sync`, `uv run`), `pnpm` for the frontend.
- Windows: open files with `encoding="utf-8"`; use absolute Windows paths in tool calls.

## Commands

```
# backend
cd backend && uv sync
uv run uvicorn server.app:app --reload --port 8000
uv run pytest
uv run asr transcribe path/to/audio.wav --provider local

# frontend
cd frontend && pnpm install
pnpm dev            # http://localhost:5173, proxies /api to :8000
pnpm gen:api        # regenerate TS client from backend OpenAPI
pnpm build

# docker
docker compose up --build        # backend (CPU) + frontend (nginx)

# desktop
cd desktop && pnpm tauri dev     # dev shell around frontend/ with the native recorder
pnpm tauri build                 # installer
```

## Conventions

- Backend: ruff (format + lint), type hints everywhere, pydantic models for all API I/O, async endpoints, tests with pytest + httpx.
- Frontend: `<script setup lang="ts">`, Pinia stores per domain, API calls only through the generated client in `src/api/`. Desktop-only features go behind `src/platform/` (`isDesktop()`); the web build must not import Tauri APIs.
- Desktop (Rust): `cargo fmt` + `clippy -D warnings`; audio capture code lives in `desktop/src-tauri/src/recorder/` and exposes Tauri commands only (no HTTP of its own except upload via the backend API).
- Commits: Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`). One logical change per commit.
- Timestamps in the data model are seconds (float). Cloud provider converts from milliseconds at the boundary.
- Speaker IDs are opaque strings (`SPEAKER_00`); display names live in `Transcript.speakers`.

## Working with agents

- Research or multi-file changes are delegated only when the user asks. Default to direct work.
- One writer per working tree. Parallel agents must own disjoint files.
- Before claiming done: run `uv run pytest` (backend) or `pnpm build` (frontend) and report the result.
