# Architecture review — `main` @ `db52182`

Inputs: `D:\tmp\PLAN_DELTA.md` (plan-vs-code delta, 2026-10-30), `PLAN.md`, `AGENTS.md`, `EXECUTION_LOG.md` D1–D58, and a boundary check of the code (import graph, provider contract, job manager, schema, IPC surface). Detailed code review is in `docs/review/` (subagent reports).

## 1. Verdict

The architecture held. The three structural bets of the plan — one `Transcript` contract behind two providers, a Web-free core library, and a separated SPA that the desktop shell reuses — are all intact in the code, and the additions made mid-project (post-correction, desktop recorder, summary) were attached at the seams the plan left open rather than by bypassing them. The main architectural debt is not in what was built but in what the plan still promises and the code does not deliver (docx, Term-sets page, OSS), and in the acceptance evidence that could not be produced on the dev machine.

## 2. Hard-rule compliance (AGENTS.md)

| Rule | Check | Result |
|---|---|---|
| 1 No host ffmpeg | `git grep` for `ffmpeg` executables / `shutil.which` in `backend/asr`, `backend/server` | None. PyAV + imageio-ffmpeg only. |
| 1a Decode-only preprocessing | `PreprocessOptions` carries channel override + level boost (+30 dB cap, skip < −60 dBFS) | Compliant; the boost is linear gain as the research allowed. |
| 2 Upload path safety | `server/storage.py` 423 lines + `test_storage.py` 307 lines; re-validation of stored paths (D54) | Covered. |
| 3 Secrets server-side | API key env-only; `_redact` in `jobs.py`; JSON logs with redaction; masked settings | Compliant, stricter than planned. |
| 4 Provider contract | `TranscriptionProvider` is a `runtime_checkable` Protocol; `test_provider_contract.py` exists | Compliant. |
| 5 Core is Web-free | `git grep "^(from\|import) (server\|fastapi\|starlette)" -- backend/asr` | **Zero hits.** |
| 6 Models local by default | All `from_pretrained(..., local_files_only=True)`; downloads only in `models.py` behind CLI / settings endpoint | Compliant. |
| 7 Python 3.12 | `requires-python = ">=3.12,<3.13"`, torch 2.14.1 pinned | Compliant. |
| 8 Recorder never loses audio | WAV patched every second, retry queue in Rust core, 300 s write-error retry (D58) | Compliant by design; acceptance partially unverified (§5). |
| 9 Non-destructive edits | `original` raw, `segments` corrected, `corrections` audit, revert route | Compliant. |
| Frontend: Tauri only behind `src/platform/` | `git grep @tauri-apps -- frontend/src` → `platform/index.ts`, `platform/tauri.ts` only | Compliant. |

## 3. Seams: how the additions attached

**[5b] post-correction.** Lives in `asr/correct/` (core). Called from exactly two places: `JobManager._post_correct` (both providers, so local and cloud transcripts get identical treatment) and `asr/cli.py`. L3 (`LlmCorrector`) and the summarizer are invoked from `routes/transcripts.py` via `run_in_threadpool`, on demand. Correct placement; see §4-c for the one concern.

**Desktop recorder.** Rust core owns capture, timeline, FLAC, uploader and retry queue; the SPA only configures through a frozen IPC contract (`desktop-ipc.md`). No new backend endpoints — uploads use the same chunked API as the browser. This is the right dependency direction: the backend knows nothing about the desktop app.

**Per-channel policy.** Changed from "skip diarization" to "diarize ch 0, fixed speaker per other channel" (D36). This is a better model for the recorder's layout (remote mix on ch 0, local mic on ch 1) and did not leak into the `Transcript` shape.

**Local stack swap** (`qwen-asr` → transformers 5.21, no torchaudio). Isolated inside `asr/providers/local/`; `models.py` is the single place that knows repo ids and mirrors. The `local` extra is cleanly separated from the `cpu`/`cuda` torch extras with a uv conflict rule. Good.

## 4. Architectural concerns

a. **Plan/code drift that is user-visible.** docx export, a Term-sets management page and OSS transfer are promised in PLAN §4/§5/§6, AGENTS.md and the Settings design, and absent. Term sets are managed only via API and the "add as alias" shortcut. These are not architectural failures, but PLAN/AGENTS now misdescribe the product. Decision needed (PLAN_DELTA Q1–Q2); recommendation: drop OSS (temp upload has covered every case; 1 GB cap is enforced by transcode), ship docx (one writer + `python-docx`, ~1 day), ship a minimal Term-sets page (CRUD already exists server-side).

b. **`server/jobs.py` is becoming the integration point for everything.** 538 lines: queue, SSE fan-out, progress batching, redaction, post-correction, persistence. It is still readable, but `_post_correct` + `_succeed` are the only places where provider output meets the correction stage and the DB; a bug there affects both providers. Suggest extracting `_post_correct`/`_succeed` into a `TranscriptFinalizer` (core-side, testable without the queue) before more stages (L2, summary-on-complete) are added.

c. **Synchronous LLM calls inside request handlers.** `llm-suggestions` and `summary` run the LLM via `run_in_threadpool` within the HTTP request. For DashScope `qwen-plus` that is seconds; for a local Qwen3 on CPU it can be minutes, holding a thread-pool slot and risking proxy timeouts (nginx default 60 s). The job queue already exists; these should become job kinds (`kind: "correct_llm" | "summary"`) with SSE progress, which also gives cancel/retry for free. Not urgent while cloud LLM is the default; required before local LLM is exposed in Settings.

d. **Single-process state.** `JobManager` holds subscribers and running state in memory; SQLite is the store. Fine for the stated scope (one backend process, single key, no users). It rules out `uvicorn --workers N`; `DEPLOYMENT.md` should state this explicitly (check in code review).

e. **Core `Transcript` has no ids; server adds them.** (D1) Keeps the core pure, but every PATCH op now does an index/id mapping in `asr/editing.py` and the SPA converts UTF-16 ↔ code points (D57). This is a reasonable trade; the risk is divergence between the three id/offset conventions (core index, server id, SPA offsets). A single shared test fixture exercising split/replace/correction offsets across all three would lock it down.

f. **Settings vs env split.** API key, bind address and endpoint allow-list are env-only; the rest is a settings table. Correct for security, but the Settings page must make the read-only fields obvious, and `asr serve` must fail with a clear message when env is incomplete (code review item).

g. **Desktop toolchain choice** (windows-gnu + WinLibs, unsigned NSIS). Pragmatic for the dev machine; a CI build on MSVC should be added before any distribution, as the `wasapi` crate and Tauri are tested upstream on MSVC.

h. **Acceptance evidence gap** is the largest risk to the "basically done" claim: CUDA path, Docker builds, real microphone call, M10 whole-device run, macOS runtime, two machines. None of these are code problems; they need hardware. See §5.

## 5. What to do next (ordered)

1. Code review (this round, subagents): backend core + providers, backend server, frontend, desktop. Reports in `docs/review/`.
2. Resolve plan drift: decide Q1–Q6 from PLAN_DELTA; update PLAN.md / AGENTS.md so they describe the shipped system; ship docx + Term-sets page or remove them.
3. Move LLM correction and summary onto the job queue (§4-c) before enabling a local LLM backend.
4. Extract `TranscriptFinalizer` from `JobManager` (§4-b) with a cross-layer offset fixture (§4-e).
5. Verification runs on real hardware, in this order of value: Docker CPU build (any Linux box with Docker — the new WSL environment may qualify), a real two-person call recorded with the desktop app and a microphone, CUDA on an NVIDIA host, M5 hand labels.
6. State the single-process constraint in `DEPLOYMENT.md`; add an MSVC CI build for the desktop app.
