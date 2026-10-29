# M11 extras — LLM summary, faster-whisper, [5b] L2, pyannote

Run 2026-10-29 on the dev box (WSL2, AMD CPU with AVX512_BF16, 16 torch threads, no CUDA). Another
backend was running acceptance tests on the same machine part of the time, so RTF values are
upper bounds.

| Item | Result |
|---|---|
| LLM summary | **Shipped.** `POST/GET/DELETE /api/transcripts/{id}/summary`, stored in schema v4, CLI `asr summarize` |
| faster-whisper English route | **Shipped as an option, not routed by default.** Qwen3-ASR is as accurate or better on English at the same or lower cost |
| [5b] L2 MacBERT4CSC (pycorrector) | **Not shipped.** Makes LLM-based ASR output worse: 0 helpful edits on any Qwen3 / Fun-ASR hypothesis |
| pyannote comparison | **Not run.** Both pipelines are gated on Hugging Face (HTTP 401 without an accepted token), and `pyannote.audio` 4 needs torchaudio, which stops at 2.11 while we pin torch 2.14.1 |

## 1. LLM summary

`asr/summarize.py` (Web-free), using the same DashScope compatible-mode chat transport, key and
endpoint allow-list as the L3 pass (`asr/correct/llm.py`, now sharing `chat_url` / `chat_json`).

- Input: the current `segments` (after edits), rendered as one line per speaker turn,
  `[mm:ss] <display name>: text`, plus the job's term glossary and context.
- Output (`TranscriptSummary`): `overview`, `key_points[]`, `decisions[]`,
  `action_items[{task, owner, due}]`, `model`, `chunks`, `created_at`. The prompt allows an
  `owner` / `due` only when the transcript says so. Speaker display names are used, so renaming
  speakers before you summarise gives named owners.
- Long transcripts: lines are grouped into chunks of about 12 000 characters at turn
  boundaries, each chunk is summarised (map), then the cleaned partial summaries are merged by
  one more call (reduce). A chunk that summarises to nothing (small talk) is skipped. A 31-min
  meeting is about 9 000 characters, so it takes one call. Hard limit: 400 000 characters.
- The model's JSON is not trusted: wrong types are dropped, strings are trimmed and capped
  (30 items, 500 characters each), and "null" / "无" owners become `null`. An empty summary is
  an error (502).
- Storage: `transcripts.summary_json` (schema v4, `NULL` until requested). Storing it does
  **not** bump `updated_at`, so `updated_at > summary.created_at` means the text changed after the
  summary (the UI can show "outdated, summarise again"). Edits and revert keep the summary.
- API: `POST /api/transcripts/{id}/summary` (no body) → `TranscriptSummary` (422 when there is no
  key or no text, 502 for upstream errors), `GET …/summary` (404 when none),
  `DELETE …/summary` (204). `TranscriptOut.summary` carries it too (nullable, backward compatible).
- CLI: `asr summarize transcript.json [-f md|json] [-o out] [-t term] [--context …] [--model …]`,
  key from `DASHSCOPE_API_KEY`.

Live check (1 call, qwen-plus, 4-min excerpt `data/local_excerpt_transcript.json`, 82 segments,
1 405 characters): 14 s. The overview and 12 key points were faithful to the conversation (horror
games, room-mates playing together); no decisions or action items, which is correct for this
excerpt. The model wrote one meta remark about the glossary ("术语表对应…"); the prompt now says not
to mention the glossary. Map/reduce is covered by mocked tests only, to stay within the cost cap.

## 2. faster-whisper English-only route

Optional extra `whisper = ["faster-whisper>=1.2"]` (adds ctranslate2 4.8.4, onnxruntime 1.31.1,
flatbuffers; no change to torch / transformers / funasr). Registry:

| Key | Source (mirror) | Size |
|---|---|---|
| `faster-whisper-small` | `Systran/faster-whisper-small` (ModelScope `Systran/faster-whisper-small`) | 0.48 GB |
| `faster-whisper-large-v3-turbo` | `dropbox-dash/faster-whisper-large-v3-turbo`, formerly `mobiuslabsgmbh/…` (ModelScope `pengzhendong/faster-whisper-large-v3-turbo`) | 1.6 GB |

`asr/providers/local/asr_whisper.py` runs int8 on CPU and float16 on CUDA, with beam 5, word
timestamps, no internal VAD (FSMN-VAD already cut the chunk), no conditioning on the previous text,
and the term list as `hotwords`. Words are returned as merge tokens, so there is no forced aligner,
and diarization and merge are unchanged. The merge now also ends a sentence at a Latin `.` that is followed
by a space or the end of the text (`3.5` and `v1.2` do not split), so English segments are
sentences, not 25 s chunks. Jobs return 422 with the extra's name when the package is missing.
A pipeline warning appears when a Whisper model runs with a language other than `en`.

### Results

**LibriSpeech** (`hf-internal-testing/librispeech_asr_dummy`, 73 dev-clean utterances, 481 s,
one reader; real speech, CC-BY-4.0). Full local pipeline per clip, `language_hints=["en"]`, one
speaker. WER is computed on normalised Latin words. "mister"→"Mr" and British/American spelling
differences count as errors for every model.

| Model | WER | RTF (incl. VAD + alignment) | Load | Segments with word times |
|---|---|---|---|---|
| faster-whisper-small (int8) | 7.27 % (85/1169) | 0.30 | 5 s | 96/96 |
| faster-whisper-large-v3-turbo (int8) | 4.02 % (47/1169) | 0.53 | 13 s | 74/74 |
| Qwen3-ASR-0.6B (bf16) + aligner | 4.11 % (48/1169) | 0.27 | 4 s | 87/91 |
| Qwen3-ASR-1.7B (bf16) + aligner | **3.42 %** (40/1169) | 0.55 | 4 s | 87/89 |

**Two-speaker dialogue** (Windows SAPI voices David / Zira, 13 turns, 86 s, technical terms such as
GitHub Actions, Docker, Kubernetes, Grafana and Prometheus; synthetic, so the audio is clean). Full pipeline with CAM++
diarization (auto speaker count).

| Model | Wall time | WER | Speakers | Speaker errors |
|---|---|---|---|---|
| faster-whisper-small | 12.4 s | 3.23 % | 2 | none; one hallucinated 0.1 s "Now?" |
| faster-whisper-large-v3-turbo | 17.0 s | 3.23 % | 2 | one 25 s chunk came back without punctuation, so three turn changes landed mid-sentence ("…who is going to own that testing I / can take it…"). "Grafina" |
| Qwen3-ASR-0.6B | 24.0 s | **1.61 %** | 2 | 4 short interjections (Okay / Perfect / One more thing / Right, about 2 s) given to the wrong speaker |

Whisper writes digits ("12 minutes") where the reference spells them out ("twelve"). Qwen3 kept the words.

### Decision

- Qwen3-ASR is already a good English recogniser here. 0.6B matches turbo's WER at half the
  cost, and 1.7B is the most accurate model on both sets. Whisper small is the only option that is
  clearly faster on longer audio (dialogue 0.14 vs 0.28 RTF), but it has about 75 % more word errors on read
  speech.
- So **no automatic routing**: `language_hints == ["en"]` keeps the default Qwen3 model.
  Recommendation for the UI or docs: "English-only and speed matters → `faster-whisper-small`;
  otherwise keep Qwen3-ASR". Whisper's padded word times also kept short interjections with
  the right speaker in the dialogue, which suggests that the aligner's tight word boundaries,
  together with `MIN_RUN_SECONDS = 0.4`, cause the interjection errors already seen in M5. That is
  worth a look in the merge, but it is not a reason to switch engines.
- No CUDA numbers (no GPU here). CTranslate2 float16 on CUDA is untested.

Reproduce: `uv sync --extra cpu --extra local --extra whisper`,
`asr models download faster-whisper-small [--mirror]`, then
`uv run --no-sync python scripts/english_eval.py --clips <dir with meta.json> --models … [--dialogue wav --dialogue-ref txt]`.

## 3. [5b] L2 generic homophone correction (MacBERT4CSC) — not shipped

`shibing624/macbert4csc-base-chinese` (Apache-2.0, 0.4 GB) is the model behind pycorrector's
`MacBertCorrector`. It was run directly with transformers (`BertForMaskedLM`), with
pycorrector's post-processing: only CJK characters, one character for one, at most 126
characters per window. pycorrector itself would add `datasets`, `pandas`, `jieba`, `loguru` and
`pyahocorasick` for no benefit. The script is `scripts/l2_macbert_eval.py`. It covers all 21 saved
bake-off hypotheses (parts 1–3: local Qwen3 0.6B / 1.7B, Fun-ASR-Nano, Paraformer, five cloud
models, with and without terms) and scores each edit by its effect on the error count against the
subtitle reference.

| Hypotheses | Min. confidence | Edits | Helped | Hurt | Neutral |
|---|---|---|---|---|---|
| LLM-based ASR (Qwen3, Fun-ASR-Nano, cloud; 19 files) | 0.0 | 84 | 4 | 43 | 37 |
| | 0.5 | 46 | 4 | 27 | 15 |
| | 0.9 | 14 | 0 | 8 | 6 |
| SeACo-Paraformer (2 files) | 0.0 | 18 | 8 | 2 | 8 |
| | 0.5 | 16 | 8 | 2 | 6 |

- CER goes up on every Qwen3 and Fun-ASR-Nano hypothesis, by 0.1–0.3 points (e.g. Qwen3-1.7B part 3 12.40 % →
  12.58 %). There is **not one helpful edit** on any Qwen3 or Fun-ASR-Nano file. The 4 helpful
  edits on LLM-based output are all 的→地 in cloud transcripts.
- Typical false positives: spoken-style particles and pronouns (吗→嘛, 它→他, 的→得), fillers
  turned into words (呃→我 / 才 / 俩), and plausible-but-wrong rewrites (录不下去→看不下去,
  像串通好→想串通好). The fixes that matter, which are terms, never come from L2: it cannot
  know 汤泡 = Townfall (it proposed 汤炮→汤泡, which is neutral). L1 and L3 already cover terms.
- On Paraformer, a weaker non-LLM model, L2 helps a little (CER 16.11 % → 15.85 %, 8 helped / 2
  hurt), but Paraformer is the fast draft mode and not the default.
- Term recall did not change for any file.

Conclusion: LLM-based ASR already gets in-context homophones right, so a general-text CSC model
mostly "corrects" spoken Chinese into written Chinese. That matches the research expectation
(docs/research/post-correction.md). Even as suggestions only, the precision on our default models
is 0 %, so it would only add review noise. Not shipped. No `Correction.source` value was added.

## 4. pyannote — not run

- `pyannote/speaker-diarization-community-1` and `pyannote/speaker-diarization-3.1` return HTTP
  401 without a Hugging Face token whose account accepted the model conditions. There is no
  token on this machine, so per the brief this item stops here.
- Dependency check, for when a token is available: `pyannote.audio` 4.0.7 requires
  `torchaudio>=2.8` and `torchcodec`. torchaudio wheels are tied to an exact torch version and stop
  at 2.11.x, while the project pins torch 2.14.1 (D9). So pyannote cannot share this venv. A
  comparison would need a separate throw-away venv (torch 2.11 + torchaudio 2.11) that writes RTTM,
  scored with `asr/eval/diarization.py` against the cloud-diarization proxy reference
  (EXECUTION_LOG §8). Shipping it as an engine would need a torch downgrade, which is not worth it
  while CAM++ is at 8.7 % proxy confusion.
- ModelScope lists repositories named `pyannote/speaker-diarization-community-1` / `-3.1` /
  `segmentation-3.0`. Using them would bypass the Hugging Face acceptance step, so they were not
  downloaded. That is the user's call (community-1 is CC-BY-4.0, 3.1 is MIT).
