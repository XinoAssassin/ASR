# Multi-Speaker ASR — Implementation Plan

## 1. Goal

Given a multi-party recording (meeting, interview, call), produce a **speaker-labelled, timestamped transcript**:

```
[00:00:03.2 - 00:00:08.7] SPEAKER_00: 今天我们先过一下上周的测试结果。
[00:00:09.1 - 00:00:12.4] SPEAKER_01: 好，我这边先说芯片那部分。
```

Deliverables:
- **Backend service** (Python / FastAPI): HTTP API; built-in local model pipeline, plus a proxy to a cloud model
- **Frontend SPA** (Vue 3): deployed independently, talks to the backend over HTTP; upload, term hints, speaker labelling, export, settings
- **CLI** (`asr transcribe meeting.wav`): same core library, for debugging and batch jobs
- **Docker images** for backend (CPU and CUDA) and frontend

## 2. Reference projects

| Project | Role | What we borrow |
|---|---|---|
| **WhisperX** (m-bain/whisperx) | Primary reference | Three-stage pipeline: transcribe → forced alignment for word timestamps → diarization → assign speakers by time overlap |
| whisper-diarization (MahmoudAshraf97) | Secondary | Vocal separation (demucs) before transcription for noisy audio |
| FunASR (Alibaba DAMO) | Chinese components | FSMN-VAD + CT-Punc + CAM++ pipeline; models without HF gating |

**Conclusion**: WhisperX-style architecture with pluggable providers. Local ASR defaults to Qwen3-ASR (far better Chinese accuracy than Whisper, `context` injection for English terms / chip names / customer names, official forced aligner); VAD and diarization use FunASR components; the cloud provider calls Alibaba `qwen-audio-3.1-asr-flash-filetrans`. Model selection rationale: `docs/ASR_MODEL_COMPARISON.md`.

## 3. Environment (probed)

| Item | State | Impact |
|---|---|---|
| GPU | Dev machine: AMD Radeon 8060S iGPU, no CUDA | PyTorch on CPU locally; `device=cuda` supported for NVIDIA hosts |
| CPU / RAM | 16 cores / 64 GB | CPU int8 inference is practical |
| Python | 3.13 installed | qwen-asr requires 3.12; funasr/pyannote unstable on 3.13 → **pin 3.12 with uv** |
| Node | 26 / pnpm 12 | Frontend toolchain ready |
| ffmpeg | Not on host, and must not be required | Bundled as a Python dependency (§4.6) |
| Models | Local models loaded from disk | ModelScope download once, then offline; cloud model needs DashScope API key and internet |

## 4. Architecture

### 4.1 Deployment topology

```
Browser ──HTTP(S)──▶ Frontend static site (nginx / any static host)
   │
   └──HTTP API (JSON, multipart, SSE)──▶ Backend FastAPI (may live on another machine, e.g. a GPU server)
                                           ├─ Provider: local  → local model pipeline (§4.3)
                                           └─ Provider: cloud  → Alibaba DashScope filetrans (§4.4)
```

- Fully separated: the frontend only knows the backend base URL (editable in Settings, persisted in the browser); the backend enables CORS with a configurable allow-list.
- The backend is network-exposed, so it carries a simple `X-API-Key` header check (set via env; empty disables it for single-machine use, in which case only loopback `Host` names / `ASR_ALLOWED_HOSTS` are accepted and cross-site writes are refused — `docs/EXECUTION_LOG.md` D24).
- Cloud calls are made by the **backend**: the DashScope key never reaches the browser, and the filetrans API requires the audio as a URL, so the server must relay the file anyway. Browser-direct cloud calls are rejected (key leakage, CORS, no file URL).

### 4.2 Provider abstraction

```python
class TranscriptionProvider(Protocol):
    def transcribe(self, audio_path, options: JobOptions, progress) -> Transcript: ...

JobOptions {
  provider: "local" | "cloud",
  language_hints: ["zh", "en"],
  num_speakers: int | None,        # None = auto
  terms: [{term, weight}],         # expanded term sets
  context: str | None,             # free-text background (agenda, names)
  preprocess: PreprocessOptions,   # §4.5
  local: {asr_model, diarize_model, device},
  cloud: {model, endpoint, keep_dialect},
}
```

Both providers emit the same `Transcript` (§5.4); the frontend never branches on provider.

### 4.3 Local pipeline (provider = local)

```
[0] Preprocess   decode → 16 kHz mono PCM (+ optional steps, §4.5)
[1] VAD          FSMN-VAD (FunASR) speech segments
[2] ASR          Qwen3-ASR 1.7B (CUDA) / 0.6B (CPU) + context term list   ← default
                 Fun-ASR-Nano-2512 + hotwords (funasr SDK)                ← fallback (faster on CPU)
                 SeACo-Paraformer                                          ← lightweight fast mode
[3] Align        Qwen3-ForcedAligner-0.6B → character timestamps (funasr route has its own; skip)
[4] Diarize      FunASR CAM++ (default) / pyannote.audio 3.1 (optional, HF token)
[5] Merge        each character/sentence takes the speaker with max time overlap; merge adjacent same-speaker runs
[6] Output       Transcript → txt / srt / vtt / json / docx
```

### 4.4 Cloud pipeline (provider = cloud, DashScope filetrans)

Async task API (sources: qianwenai.com model page and Alibaba Model Studio docs; **parameter names to be verified against the official docs at implementation time**):

```
1. Obtain an audio URL (strategy chosen in Settings; decision record: docs/research/file-transfer-for-cloud-asr.md)
   a. DashScope temporary upload  ← DEFAULT. GET /api/v1/uploads?action=getPolicy&model=… → OSS PostObject form
      upload → `oss://` URL, 48 h, bound to the same model + account, 1 GB interface cap (read max_file_size_mb
      from the policy; transcode to 16 kHz mono FLAC/MP3 first if the source is larger). Submit via REST only
      (SDK does not accept oss:// URLs) with header `X-DashScope-OssResourceResolve: enable`.
   b. Alibaba OSS (oss-cn-beijing) + presigned GET (≈6 h) + 1-day lifecycle rule  ← production fallback
   c. User-supplied public URL (skips the local file)
   Rejected: Cloudflare R2 / Backblaze B2 (reachability from Alibaba Beijing unverified — may be offered later
   after an empirical probe), Cloudflare Tunnel / Tailscale Funnel / ngrok (bandwidth caps, ToS, cross-border).
2. POST {endpoint}/api/v1/services/audio/asr/transcription
   headers: Authorization: Bearer $DASHSCOPE_API_KEY, X-DashScope-Async: enable
   body: { model: "qwen-audio-3.1-asr-flash-filetrans",
           input: { file_urls: [url] },
           parameters: { diarization_enabled: true, speaker_count?, vocabulary: {term: weight},
                         language_hints: ["zh","en"], keep_dialect: false, channel_id: [0] } }
   → task_id
3. GET {endpoint}/api/v1/tasks/{task_id} every 3–5 s until SUCCEEDED / FAILED
4. Download results[].transcription_url JSON (sentences: begin_time / end_time / text / speaker_id, optionally words)
5. Normalise into Transcript
```

- Endpoint is selectable: `https://dashscope.aliyuncs.com` (Model Studio) or `https://cn-beijing.maas.qianwenaiapi.com` (Qianwen AI platform, as shown on the model page). Keys are not interchangeable.
- The cloud model already does diarization, hotwords and punctuation, so local stages [1]–[5] are skipped. Term sets map to `vocabulary` (weight 1–5, boosted terms 50).
- Pricing on the model page (2026-10): ¥0.8 / M input tokens, ¥2.7 / M output tokens. Settings shows a "audio will be uploaded to Alibaba Cloud" notice.

### 4.5 Audio preprocessing

Decision record: `docs/research/audio-preprocessing.md`. Finding: WhisperX, FunASR, pyannote and NeMo all do exactly one mandatory step (decode to 16 kHz mono); three independent studies (arXiv 2201.06685, 2512.17562, 2603.04710) show single-channel enhancement in front of modern large-scale ASR **raises** WER in every tested configuration. Therefore nothing non-linear runs by default.

Default chain (always on):
```
[0a] probe            duration, sample_rate, channels, codec (PyAV stream metadata)
[0b] channel policy   1ch → as is; 2ch correlated → average; 2ch independent (telephony heuristic) → per-channel
                      pipeline, skip diarization (cloud: channel_id=[0,1]); >2ch → average or user-picked index
[0c] decode           → 16 kHz mono float32 in [-1, 1] (8 kHz sources upsampled in the same pass)
[0d] peak-safety gain if peak < -20 dBFS, linear gain to -3 dBFS; no compression, no loudnorm
[0e] clipping check   warning only, surfaced in the job UI
[1b] long-file chunks VAD-aligned ~5-min windows for ASR/ForcedAligner (its limit); ~20–30-min windows with 30 s
                      overlap for CAM++/pyannote, speakers reconciled across windows by embedding cosine similarity;
                      cloud path split at VAD silence to ≤ 2 h when diarization is enabled (DashScope cap)
```

Optional toggles (`PreprocessOptions`, all off by default; UI copy states that denoising usually does not improve accuracy; each benchmarked in M4 against raw audio before being surfaced):

| Toggle | Use case | Note |
|---|---|---|
| `denoise: deepfilternet` | steady HVAC/fan noise | 16k→48k→DFN3→16k, blend with original; keep raw audio for diarization |
| `denoise: clearvoice_frcrn_16k` | same, Chinese-centric stack (ModelScope) | no 48 k round trip; CPU RTF to be measured |
| `vocal_separation: demucs` | music beds (podcasts, event videos) | whisper-diarization precedent; 10–30 min per hour on CPU |
| `dereverb: wpe` | far-field conference-room mic | linear, low artifact risk |
| `loudness: ebu_r128` | very uneven speaker levels | 2-pass loudnorm; raises noise floor in pauses |
| `channels: left/right/per-channel/downmix` | user knows the recording layout | overrides the heuristic |
| `resemble_enhance` | — | not offered: generative, alters timbre, hours per hour on CPU |

Expected input sources (`docs/research/recording-sources.md`): iPhone Voice Memos (AAC 64 kbps mono 48 kHz, ~29 MB/h; iPhone 16+ stereo → correlated downmix; Lossless ALAC may exceed the 1 GB cloud cap after ~4 h), iOS 18+ call recordings (2-ch AAC 256 kbps, party separation unverified → run the correlation heuristic, offer override), Apple Watch (AAC ~65 kbps mono, ~20 min practical cap, reverberant → peak gain default, WPE optional), Windows loopback (48 kHz float32 stereo WAV hits 1 GB at ~43 min → transcode to 16 kHz FLAC before cloud upload; OBS mic + desktop on separate tracks → per-channel policy, no diarization on the mic channel). Meeting-tool native recordings (Zoom per-participant M4A, Teams/Tencent/Feishu cloud recordings with transcripts) are preferable when available; importing their speaker labels is a future job type.

### 4.6 Bundled ffmpeg

Decision record: `docs/research/bundled-ffmpeg.md`. Hard rule (AGENTS.md): never call a host `ffmpeg` from `PATH`; the Dockerfile installs no OS-level ffmpeg.

- **Primary: PyAV (`av>=19`)** — wheels bundle FFmpeg 9.x shared libraries for win_amd64 / win_arm64, manylinux and musllinux x86_64 + aarch64, macOS; Python ≥ 3.12 abi3. In-process API: `AudioResampler(format="s16", layout="mono", rate=16000)` for decode, stream metadata for probing, `pan` filter for channel extraction, encoder API to pre-transcode MP3/Opus for the browser player (served via `FileResponse` so Range seeking works). Blocking CPU work → `run_in_threadpool`.
- **Fallback: imageio-ffmpeg** — static `ffmpeg` CLI inside the wheel (no `ffprobe`), driven via `subprocess`; used if a PyAV native-extension problem appears on some platform. Selected by `ASR_DECODER=pyav|imageio`.
- Both are GPL-configured builds (x264/x265 linked). Acceptable for a self-hosted service; revisit only if a proprietary desktop installer is ever shipped.
- Not usable alone: `soundfile` / `miniaudio` (no AAC/M4A/MP4); `pydub`, `audioread`, `python-ffmpeg`, `torchcodec` rely on host ffmpeg.

### 4.7 Code layout

```
backend/
  asr/                    # core library, no Web dependencies
    audio/
      decode.py           # bundled-ffmpeg decode / probe / channel select
      preprocess.py       # resample, normalise, optional denoise / separation
    providers/
      base.py             # TranscriptionProvider, JobOptions, Transcript
      local/
        pipeline.py       # [0]–[6] with progress callbacks
        vad.py
        asr_qwen3.py
        asr_funasr.py
        diar_campplus.py
        diar_pyannote.py
        align.py
        merge.py
      cloud/
        dashscope_filetrans.py   # upload / submit / poll / normalise
        transfer/                # temp_upload.py, oss.py, public_url.py
    writers.py            # txt / srt / vtt / json / docx
    cli.py                # typer CLI
  server/
    app.py                # FastAPI, CORS, X-API-Key
    config.py             # env + settings table
    jobs.py               # queue: single worker for local jobs, concurrent cloud jobs
    storage.py            # upload-root validation and persistence
    routes/               # uploads / jobs / transcripts / term_sets / settings / fs
    db.py                 # SQLite (aiosqlite)
  tests/
frontend/                 # Vue 3 + Vite + TS + Naive UI + wavesurfer.js
  src/api/                # generated from backend OpenAPI
  src/views/              # Upload / Jobs / Transcript / TermSets / Settings
deploy/                   # nginx.conf, frontend-entrypoint.sh
docs/
  ASR_MODEL_COMPARISON.md
  research/
tests/fixtures/           # 2–3 short multi-speaker clips (not in git)
Dockerfile, docker-compose.yml, .env.example, AGENTS.md, PLAN.md
```

## 5. Backend API

### 5.1 Upload and storage

- The frontend chooses the **server-side target directory**: form field `target_dir` on `POST /api/uploads` (absolute path or path relative to an allowed root).
- Backend validation: the resolved path must lie inside one of `ASR_UPLOAD_ROOTS` (default `./data/uploads`); reject `..` and symlink escapes; create the directory if missing. Filenames are stripped of path separators; collisions get a numeric suffix.
- `GET /api/fs/roots` returns the allowed roots; `GET /api/fs/dirs?path=` lists subdirectories for the directory picker; the frontend remembers recently used directories.
- Chunked upload for large files: `POST /api/uploads/init` → `PUT /api/uploads/{id}/parts/{n}` → `POST /api/uploads/{id}/complete`; 2 GB cap.
- Optional `POST /api/uploads/register {path}` registers a file that already exists on the server (no transfer).
- On completion the backend probes duration / sample rate / channels and returns `upload_id` and the stored path.

### 5.2 Jobs

```
POST   /api/jobs                      {upload_id | file_url, provider, options: JobOptions, term_set_ids, extra_terms}
GET    /api/jobs?status=&page=
GET    /api/jobs/{id}                 status, stage, progress, error
GET    /api/jobs/{id}/events          SSE progress (local: decode → VAD → ASR → diarize → merge; cloud: upload → queued → recognising)
POST   /api/jobs/{id}/cancel
POST   /api/jobs/{id}/retry
DELETE /api/jobs/{id}                 optionally delete the audio too
```

### 5.3 Transcripts, term sets, settings

```
GET    /api/transcripts/{id}
PATCH  /api/transcripts/{id}          incremental ops: edit_text / set_speaker / split / merge_segments
PUT    /api/transcripts/{id}/speakers rename / merge speakers
POST   /api/transcripts/{id}/revert   back to the original recognition result
GET    /api/transcripts/{id}/export?format=txt|srt|vtt|json|docx&timestamps=1&merge_same_speaker=1
GET    /api/audio/{upload_id}         streamed audio with Range support (browser-friendly transcode if needed)

CRUD   /api/term-sets                 term sets; POST /api/term-sets/import (txt / paste)

GET    /api/settings                  secrets masked
PUT    /api/settings
POST   /api/settings/test-cloud       verify the DashScope key
GET    /api/settings/local-models     local model download status; POST .../download
GET    /api/health
```

### 5.4 Data model

```
Upload     { id, path, original_name, duration, sample_rate, channels, size, created_at }
Job        { id, upload_id, provider, options, status: queued|running|succeeded|failed|cancelled,
             stage, progress, error, cloud_task_id?, created_at, finished_at }
Transcript { id, job_id, provider, speakers: {id, name, color}[], segments: Segment[], original: Segment[] }
Segment    { id, speaker_id, start, end, text, words: {text, start, end}[], edited: bool }
TermSet    { id, name, terms: {term, weight, note}[] }
Settings   { provider_default, api_key_required,
             local: {asr_model, diarize_model, device, model_cache_dir},
             cloud: {endpoint, model, api_key, file_transfer: temp_upload|oss|public_url, oss: {...}} }
```

Timestamps are seconds (float) throughout; the cloud provider converts from milliseconds at the boundary.

## 6. Frontend

### 6.1 Stack

| Layer | Choice | Rationale |
|---|---|---|
| Framework | Vue 3 + Vite + TypeScript | Standalone SPA, pure static build |
| UI kit | Naive UI | Complete form / table / upload / tree components, good Chinese docs |
| Waveform | wavesurfer.js + regions | Speaker-coloured regions, click to seek |
| State | Pinia + vue-router | — |
| API client | openapi-typescript generated from the backend OpenAPI | Single source of truth for the contract |
| Backend URL | Settings page, stored in localStorage; Docker image injects a default via `config.js` | Cross-machine deployment |

### 6.2 Pages

**Upload**
- Drag-and-drop, multi-select; accepted: m4a / mp3 / wav / flac / aac / caf / ogg / opus / webm / mp4 / m4v / mov / mkv (PyAV probes; codec / rate / channels / duration shown before the job starts); chunk progress
- Detected channel policy (mono / stereo-downmix / per-channel) with one-click override; OBS multi-track files get an audio-track selector
- Size hint for cloud: files over the temp-upload cap are transcoded to 16 kHz FLAC first; estimated post-transcode size shown
- Collapsible source hints (iPhone: keep "Compressed", choose "Standard" mic mode for meetings, do not export with effects; Apple Watch: ≤ 20 min per clip; Windows: OBS with mic on track 1 and desktop audio on track 2, prefer the meeting tool's own recording)
- Directory picker: root dropdown + subdirectory tree (`/api/fs/dirs`) + manual path + recent
- Job options: provider (local / cloud, default from Settings), language hints, speaker count (auto / fixed), term sets + ad-hoc terms, context text, preprocessing toggles
- "File already on server" path registration

**Jobs**
- Status, stage progress bar, provider, duration, elapsed; cancel / retry / delete
- SSE live updates; state survives browser restarts

**Term sets**
- Multiple sets (per customer / project); term + weight + note; paste import, txt import/export
- Explanation of cloud weight mapping (1–5 normal, 50 boosted, max 50 boosted)

**Transcript**
- Waveform + player on top, dialogue stream below; click to seek; current sentence highlighted; shortcuts (space play/pause, ← → previous/next sentence)
- Speakers: rename (global), fixed colours, reassign a segment, merge speakers, split a segment at a character timestamp
- Inline text editing with autosave; revert to original
- Term highlighting; "replace across transcript by term set" for homophone fixes
- Search
- Export: txt / srt / vtt / json / docx; options: timestamps, merge consecutive same-speaker

**Settings**
- Backend URL, X-API-Key
- Default provider
- Local: ASR model, diarization model, device (cpu / cuda), model cache dir, download status and one-click download
- Cloud: endpoint, model name, DashScope key (masked), file transfer method (temp upload / OSS / public URL) and its credentials, "test connection"
- Privacy notice for cloud mode

## 7. Milestones

| # | Scope | Acceptance |
|---|---|---|
| M0 | Scaffold: uv + Python 3.12, bundled ffmpeg dependency, `backend/` pyproject + empty FastAPI app, `frontend/` Vite skeleton, OpenAPI client generation script, Dockerfile builds | `/api/health` reachable; frontend blank page connects; `docker compose up` starts both |
| M1 | Backend basics: upload (target dir + validation + chunks), job queue + SSE, SQLite, Transcript model, settings, X-API-Key, decode/probe via bundled ffmpeg | curl can upload and create a job |
| M2 | Cloud provider: file transfer (default strategy from research) → submit → poll → normalise; term sets → vocabulary | A real recording yields a speaker-labelled Transcript via the cloud |
| M3 | Frontend v1: Upload (with directory picker), Jobs, Transcript (waveform + dialogue + seek), export, Settings (backend URL / provider / cloud key) | Browser flow: upload → cloud transcribe → view → export |
| M4 | Local ASR bake-off: 3–5 min of own audio across Qwen3-ASR-0.6B/1.7B, Fun-ASR-Nano, SeACo-Paraformer (CER / term recall / RTF); pick default | Results table in `docs/` |
| M5 | Local provider: preprocess + VAD + ASR + align + CAM++ + merge wired into the queue; CLI wrapper | Local output has the same Transcript shape as cloud; speaker mis-assignment < 10% |
| M6 | Term sets both ways (local context/hotwords, cloud vocabulary); local model management in Settings; CUDA verification; preprocessing toggles in UI | Visible term-recall gain; `device=cuda` works on an NVIDIA host |
| M7 | Frontend v2: speaker rename / merge / reassign / split, autosave + revert, term highlight + replace, search, shortcuts | Proofreading a 10-min recording without leaving the page |
| M8 | Polish and deployment: production Docker images, `asr serve`, batch mode, logging, deployment guide | Backend and frontend run on two separate machines |
| M9 (optional) | pyannote comparison, faster-whisper English-only backend, LLM correction and summary | — |

Order rationale: the cloud provider comes first (M2–M3) because it gives the fastest end-to-end path and fixes the Transcript contract; the local pipeline (M4–M6) is built against the same contract, so the frontend does not change.

## 8. Key dependencies

```
# backend
fastapi, uvicorn, sse-starlette, python-multipart, aiosqlite, pydantic-settings, httpx
av>=19                          # PyAV, bundled FFmpeg (docs/research/bundled-ffmpeg.md)
imageio-ffmpeg                  # static ffmpeg CLI fallback
qwen-asr                        # Qwen3-ASR + ForcedAligner, default local ASR (Python 3.12)
funasr, modelscope              # FSMN-VAD / CAM++ diarization; Fun-ASR-Nano / Paraformer fallback ASR
torch                           # CPU wheel locally; cu12x wheel on NVIDIA hosts (pyproject extras: cpu / cuda)
httpx                           # cloud provider (REST; the dashscope SDK does not accept oss:// temp URLs)
oss2                            # Alibaba OSS transfer fallback
typer, rich                     # CLI
python-docx                     # docx export
faster-whisper, pyannote.audio>=3.1   # M9

# frontend
vue 3, vite, typescript, naive-ui, wavesurfer.js, pinia, vue-router, openapi-typescript
```

Default local models (ModelScope, cached after first download):
- ASR: `Qwen/Qwen3-ASR-1.7B` (CUDA) / `Qwen/Qwen3-ASR-0.6B` (CPU)
- Alignment: `Qwen/Qwen3-ForcedAligner-0.6B`
- Fallback ASR: `FunAudioLLM/Fun-ASR-Nano-2512`, `iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch`
- VAD: `iic/speech_fsmn_vad_zh-cn-16k-common-pytorch`
- Diarization: `iic/speech_campplus_sv_zh-cn_16k-common`

## 9. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Cloud filetrans accepts URLs only | DashScope temp upload by default (1 GB cap → transcode first), Alibaba OSS as production fallback; strategies behind one interface, switchable in Settings |
| Temp upload documented as "not for production" | Settings shows the caveat; OSS path available for production use |
| Cloud parameter names / response shape differ from this plan | Implement against official docs; normalise inside the provider; frontend unaffected |
| Cloud privacy and cost | Explicit notice in Settings; provider shown per job; default provider can be local |
| Path traversal via `target_dir` | Only paths under `ASR_UPLOAD_ROOTS`, realpath check, symlink escape rejected, unit-tested |
| Network-exposed backend | X-API-Key + CORS allow-list; HTTPS reverse proxy recommended in production |
| Host ffmpeg drift | Bundled ffmpeg dependency pinned in `uv.lock`; no OS ffmpeg in Docker |
| English terms recognised as homophone Chinese | Local `context` / `hotwords`, cloud `vocabulary`; transcript-wide replace in M7; optional LLM correction |
| Qwen3-ASR repeats hotwords on similar-sounding audio (issue #140, streaming) | Offline mode; term lists ≤ 200 entries; repetition post-filter |
| No CUDA locally; Qwen3-ASR-1.7B slow on CPU | 0.6B locally; switch to Fun-ASR-Nano / Paraformer if too slow; heavy jobs on a GPU host or the cloud |
| Overlapping speech | Accept single-label output first; overlap-aware handling later |
| CAM++ diarization weaker than pyannote | Compare in M9 |
| Python 3.13 incompatibilities | uv pins 3.12 via `.python-version` |
| Browser disconnects during long jobs | Server-side queue, state in SQLite, SSE reconnect |
| Edits vs character timestamps | Text edits never recompute timestamps; splits use character timestamps; edited segments interpolate proportionally |

## 10. Decisions taken

- Language: Chinese-primary with English terms → local default Qwen3-ASR; cloud `qwen-audio-3.1-asr-flash-filetrans`; term sets injected on both paths.
- Deployment: separated frontend and backend over HTTP; backend may run on a GPU host; CPU on the dev machine; Docker images for both.
- Storage: frontend picks the server directory; backend persists inside allowed roots.
- Providers: local and cloud, switchable in Settings, overridable per job.
- Cloud calls are relayed by the backend (key never reaches the browser).
- ffmpeg is a bundled Python dependency (PyAV primary, imageio-ffmpeg fallback); host ffmpeg is never used.
- Preprocessing is decode-only by default; denoise / separation / loudness are opt-in toggles.
- Cloud file transfer: DashScope temporary upload is sufficient (confirmed); Alibaba OSS kept as an optional fallback; R2 and tunnels not offered.
- All documentation in English; UI strings Chinese-first with i18n.

## 11. Open questions

1. **Cloud endpoint**: Model Studio `dashscope.aliyuncs.com` or Qianwen AI `cn-beijing.maas.qianwenaiapi.com`? Keys are not interchangeable.
2. **Access scope**: LAN multi-user with login and per-user job isolation? Currently shared X-API-Key, no user separation.
3. **Upload roots**: which server directories should be allowed (e.g. `D:\ASR\uploads`, a NAS mount)?
4. **Typical audio length**: affects chunk size and cloud polling strategy.
5. **Term list**: is there an existing list of English terms / chip names / customer names to seed the first term set?
