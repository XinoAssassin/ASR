# Kickoff prompt for Claude Code (Opus 5.5) with crew / subagents

Copy everything below the line into a fresh Claude Code session started in `D:\Develop\ASR`.

---

You are the lead engineer on this repository. Work in `D:\Develop\ASR` (git, branch `main`). Planning is finished; your job is to implement milestones **M0 → M3** of `PLAN.md` using subagents for parallel, non-overlapping work, and to verify everything before reporting.

## Read first (in this order, fully)

1. `AGENTS.md` — hard rules. They are not negotiable.
2. `PLAN.md` — architecture, API contract (§5), data model (§5.4), milestones (§7).
3. `docs/research/bundled-ffmpeg.md` — PyAV primary / imageio-ffmpeg fallback, with a decode+probe code sketch.
4. `docs/research/audio-preprocessing.md` — decode-only default chain; toggles are off by default.
5. `docs/research/file-transfer-for-cloud-asr.md` — DashScope temporary upload is the default and sufficient; note the 1 GB cap, REST-only, `X-DashScope-OssResourceResolve: enable`.
6. `docs/research/recording-sources.md` — input characteristics (iPhone / Apple Watch / Windows loopback); drives the accepted-extension list, channel policy UI, and the transcode-before-upload rule.
7. `Dockerfile`, `docker-compose.yml`, `.env.example`, `deploy/` — already scaffolded; keep them consistent with what you build.

Do not re-research model selection, preprocessing, ffmpeg bundling, or transfer strategy; decisions are recorded in PLAN §10.

Test asset: `ASR_4people-test.m4a` in the repo root (27 MB, 4-speaker Chinese meeting clip, git-ignored). Use it for the M2 end-to-end cloud test and as the first fixture for probe/decode tests (copy a 20-second excerpt into `tests/fixtures/` for unit tests; never commit the full file).

## Scope of this run

| Milestone | Deliverable | Acceptance |
|---|---|---|
| M0 | `backend/` (uv, Python 3.12, pyproject with `cpu` / `cuda` extras, FastAPI app, ruff, pytest), `frontend/` (Vue 3 + Vite + TS + Naive UI + Pinia + vue-router, OpenAPI client generation), both Docker targets build | `GET /api/health` works; `pnpm dev` blank page reaches the backend; `docker compose build` succeeds |
| M1 | Uploads (target_dir validation against `ASR_UPLOAD_ROOTS`, chunked upload, register-existing-file), `GET /api/fs/roots|dirs`, SQLite via aiosqlite, job queue with SSE progress, settings (secrets masked), `X-API-Key` middleware, PyAV decode/probe module incl. channel-correlation heuristic (mono / downmix / per-channel) and the accepted-extension list from recording-sources.md, `GET /api/audio/{id}` with Range (pre-transcoded MP3/Opus) | pytest green incl. path-traversal and probe tests; curl flow upload → job works on `ASR_4people-test.m4a` |
| M2 | Cloud provider `asr/providers/cloud/dashscope_filetrans.py`: getPolicy → OSS form POST → `oss://` URL → transcode to 16 kHz mono FLAC if over `max_file_size_mb` → submit (REST, `X-DashScope-OssResourceResolve: enable`, `diarization_enabled`, `channel_id` from channel policy) → poll → fetch result JSON → normalise to `Transcript` (ms → s); term sets → `vocabulary`; provider contract test with recorded HTTP fixtures (respx) | `ASR_4people-test.m4a` produces a 4-speaker Transcript when `DASHSCOPE_API_KEY` is set; offline tests pass without the key |
| M3 | Frontend v1: Upload (directory picker, probe summary, channel-policy display with override, cloud size hint, collapsible source tips in Chinese, job options), Jobs (SSE), Transcript (wavesurfer waveform + dialogue stream + click-to-seek), export (txt/srt/vtt/json), Settings (backend URL, API key, provider, cloud endpoint + key masked, test-connection) | Browser flow upload → cloud transcribe → view → export on the test clip; `pnpm build` and `pnpm typecheck` clean |

Out of scope now: local model pipeline (M4–M6), preprocessing toggles beyond the default chain, speaker editing UI (M7), docx export, OBS multi-track selector, meeting-tool transcript import. Leave clean extension points (`TranscriptionProvider`, `PreprocessOptions`, `channel_policy`) but do not implement them.

## How to work

**Phase 0 — orient (you, no subagents).** Read the files above. Produce a short written task breakdown in `docs/EXECUTION_LOG.md` listing each task, its owner subagent, and the exact files it owns. Files must not overlap between concurrently running agents. Commit it.

**Phase 1 — scaffold (one executor).** M0 end-to-end in a single agent so the project layout, lint/test config, and OpenAPI generation are coherent. Verify, commit.

**Phase 2 — parallel implementation (crew).** Launch in parallel, each with disjoint file ownership:
- `backend-core`: storage + fs routes + upload routes + audio decode/probe/channel heuristic/transcode + tests (`backend/asr/audio/**`, `backend/server/storage.py`, `backend/server/routes/{uploads,fs,audio}.py`, matching tests)
- `backend-jobs`: db, job queue, SSE, settings, API-key middleware, transcript routes + export writers (`backend/server/{db,jobs,config,app}.py`, `backend/server/routes/{jobs,transcripts,settings,term_sets}.py`, `backend/asr/writers.py`, tests)
- `cloud-provider`: `backend/asr/providers/**` incl. base contract, DashScope client, temp upload, normalisation, respx-based tests
- `frontend`: everything under `frontend/` against the OpenAPI spec exported by Phase 1 (regenerate the client once backend routes land)

Shared contracts (`Transcript`, `JobOptions`, route paths) are fixed by PLAN §5; if an agent needs to change a contract it must stop and report to you rather than edit another agent's files.

**Phase 3 — integrate and verify (you + one verifier).** Merge the work, regenerate the TS client, run: `uv run ruff check`, `uv run pytest`, `pnpm typecheck`, `pnpm build`, `docker compose build`. Then an independent verifier agent re-runs the acceptance table above from a clean checkout and reports PASS/FAIL per row with evidence (command output). Fix failures; do not mark done on a FAIL.

**Phase 4 — review (one reviewer, read-only).** Security-focused pass on: path traversal in `target_dir`, API-key handling and masking, CORS config, SSRF on `file_url` job input, secrets in logs. Address findings, commit.

## Rules of engagement

- Conventional Commits, one logical change per commit, commit after each phase at minimum.
- Never call host `ffmpeg`; use PyAV (`av`) with imageio-ffmpeg fallback as in the research doc. Preprocessing is decode-only by default (no denoise / loudnorm).
- Python 3.12 pinned via `.python-version` and `uv.lock`; Windows dev machine, no CUDA — tests must run on CPU and must not download models.
- All code comments, docs and commit messages in English; UI strings in Chinese with i18n keys.
- Secrets only from env / settings table; never in the browser, never in logs, never in git.
- When something in PLAN.md is ambiguous, choose the simplest option that satisfies the acceptance table, note the decision in `docs/EXECUTION_LOG.md`, and continue. Ask me only if a decision changes the API contract or costs money.
- Cloud calls cost money: run the live DashScope test on the test clip at most a few times; everything else goes through recorded fixtures.
- Before claiming completion, paste the verifier's PASS/FAIL table and the final `git log --oneline`.

Start with Phase 0.
