# Core library & providers review (main @ db52182)

Scope: `backend/asr/providers/{base,local/**,cloud/**}`, `backend/asr/audio/**`, `backend/asr/correct/**`, `editing.py`, `writers.py`, `summarize.py` and their tests. Read-only, checked against AGENTS.md hard rules, PLAN §4.5 / §6.3 and EXECUTION_LOG D29–D41 (plus D20 and D57, which those decisions depend on).

## Summary

| # | Sev | Area | Finding | Location |
|---|---|---|---|---|
| M1 | **Major** | merge (D35) | Text outside token spans is silently dropped at segment boundaries (leading `《“$`, trailing `++`, `℃`, emoji); sentence ends after symbols are missed | `local/merge.py:231-239, 311-312` |
| m1 | Minor | cloud per_channel (D36) | Cloud per_channel never diarizes channel 0, so desktop recordings lose remote-speaker separation, and no warning is given | `cloud/dashscope_filetrans.py:300, 403-404`; `cloud/normalize.py:146-148` |
| m2 | Minor | cloud URL sources | URL jobs turn diarization on for `right` / `auto` on possibly stereo audio, which contradicts the reasoning behind D20 | `dashscope_filetrans.py:343-351, 300` |
| m3 | Minor | cloud language hints | Hints go to DashScope as typed (`auto`, `zh-CN`, `Chinese`); the local provider normalises the same hints | `dashscope_filetrans.py:166` vs `local/pipeline.py:352-372` |
| m4 | Minor | cloud polling | About 31 s of transient poll failures aborts a job that can run for hours. The remote task keeps running and is billed, and a retry starts a new task | `cloud/client.py:163`, `dashscope_filetrans.py:530` |
| m5 | Minor | diarization (D34) | An undocumented 20-window floor folds any speaker with < ~15 s of speech on recordings under ~8 min | `local/diar_campplus.py:40, 62` |
| m6 | Minor | VAD chunking (D34) | VAD segments over 25 s are split evenly with no overlap, so the cuts land mid-word | `local/vad.py:25-30` |
| m7 | Minor | L3 LLM guard (D44) | A change whose `after` is a glossary term is exempt no matter how long `before` is, so the 15 % guard can be bypassed | `correct/llm.py:58-61` |
| n1 | Nit | merge | Fold asymmetry between text and needle in `token_spans` (`½`, `ﬁ`, `İ`) makes the whole 25 s chunk fall back to a single speaker | `local/merge.py:101, 105` |
| n2 | Nit | resources | `LlmCorrector` / `Summarizer` create an `httpx.Client` they never close (CLI path) | `correct/llm.py:219`, `summarize.py:184`, `cli.py:487` |
| n3 | Nit | local engines | The engine cache is cleared only after the new model set has loaded, so two sets are resident at peak | `local/pipeline.py:145` |
| n4 | Nit | cloud errors | A 403 on the signed result download is reported as "DashScope rejected the API key" | `cloud/client.py:123, 242-249` |
| n5 | Nit | per_channel ids | A silent channel still uses up a speaker id (local), and numbering differs from cloud | `local/pipeline.py:235-238`, `cloud/normalize.py:146-148` |
| n6 | Nit | [5b] L1 | Corrected segments keep stale `words`, which no longer tile the text | `correct/__init__.py:181` |

No Critical findings. **Verdict: OK with notes.** Fix M1 before release, because it loses text in the immutable `original`.

## Correct (verified)

- **ms → s boundary (cloud).** `normalize._ms` handles int, float, numeric strings and negatives (clamped to 0). `_seconds` rounds to ms. Word times are clamped into their sentence. Duration is the larger of the result duration and the last segment end. No provider fields leak into `Transcript`, and the contract test checks this (`test_provider_contract.py`).
- **Code-point offsets (D57).** `_EditLog.replace` (`correct/__init__.py`) rebuilds `before` across overlapping earlier edits and shifts later offsets by the length change. The test that rebuilds raw text from the corrections right-to-left passes by construction. `EditState._mutate` / `split` / `merge` rebase offsets or drop them (`None`); review applies strictly at the offset.
- **Merge invariants (D35).**
  - Segment `end` is capped at the chunk end and floored at `start + 0.08` s.
  - `snap_changes` keeps at least one token in every run, using a `prev + 1` / `nxt - 1` window.
  - `absorb_short_runs` terminates because every pass removes a run.
  - With no tokens, a chunk falls back to one segment chosen by overlap.
  - A single-speaker track sets `chunk.speaker`, which skips the turn lookup.
- **Per-channel policy (D36, local).** Channel 0 is diarized; every other channel gets a fixed speaker. `oracle = num_speakers - fixed_tracks`, and diarization is skipped when that is ≤ 1. `num_speakers == 1` disables diarization. Tests: `test_per_channel_*`, `test_per_channel_oracle_excludes_fixed_channels`.
- **Empty VAD / degenerate input.** No speech gives no chunks and no diarizer call. If the diarizer returns `[]`, the fallback speaker is still allocated. Chunks under 100 ms are skipped by all three ASR engines and by the aligner.
- **Overlapping segments.** Each track's segments are built separately, then sorted globally by `(start, end)` and renumbered. Progress stays monotonic across tracks (`base + fraction * share`).
- **DashScope client.**
  - `X-DashScope-OssResourceResolve: enable` is sent only for `oss://` uploads. Tests cover both cases (`test_cloud_provider.py:93, 160`).
  - Submit retries only on 429 and on connect/pool errors; 5xx and read timeouts are not retried (could double-bill).
  - Polling retries 429, 5xx and timeouts. `Retry-After` is honoured, capped at 60 s.
  - `cancel_task` never raises and runs on JobCancelled, KeyboardInterrupt and the overall timeout.
  - `FAILED`, `UNKNOWN` and `CANCELED` are mapped, and partial subtask failures produce a warning.
  - Result downloads are https `*.aliyuncs.com` only, without the auth header, with no redirects. Errors are redacted (key, signed query strings, `sk-`).
- **1024 MB cap and transcode fallback (D8 / D20).**
  - The cap is `policy.max_file_size_mb` from getPolicy.
  - Transcoding is triggered by: over the cap, a downmix of a multi-channel file, a `select` channel (extracted to mono with diarization on), or `stream_index != 0`.
  - If the FLAC is still too big and has ≤ 2 channels, it is re-encoded as MP3 at 64 kbit/s (mono) or 96 kbit/s (stereo); otherwise the job fails with a clear message.
  - The policy is refreshed with a 60 s margin. Upload retries get a fresh policy and object key. Temp files are removed in `finally`.
- **Level-boost gain math** (`audio/preprocess.py`).
  - Applied only when the peak is in [-60, -20) dBFS.
  - `gain_db = min(-3 - peak_db, 30)`, so a -20 dBFS peak gets 17 dB and a -50 dBFS peak gets 30 dB (ending at -20).
  - The output peak is ≤ -3 dBFS, so it cannot clip, and the input array is not modified.
  - Applied per track. Tests: `test_audio_transcode.py:172-220`.
- **Resource handling.**
  - Every `av.open` is inside a `with` (`_meta_pyav`, `_packet_duration_pyav`, `_iter_pyav`, `_transcode_pyav`). Returning early at `max_seconds` closes the container.
  - The imageio decoder sends stderr to a temp file (no pipe deadlock) and kills and waits for the process in `finally`.
  - Transcodes write a temp file, then `os.replace`, and unlink on any exception.
- **Hard rules.**
  - Rule 1: `bundled_ffmpeg_exe` refuses any binary outside the imageio-ffmpeg wheel and never calls a bare `ffmpeg`.
  - Rule 5: no `server` / `fastapi` / `starlette` imports anywhere under `asr/` (grep).
  - Rule 6: every `from_pretrained` passes `local_files_only=True`; funasr models load from `model_dir()` paths, which raise if missing; `snapshot_download` exists only in `models.download`.
- **Writers.** Txt timestamps round half-up to tenths (via whole ms). SRT/VTT use ms. VTT escapes `&<>` (so `-->` cannot appear in a cue). Names and text are collapsed to one line.

## Findings

### M1 — Major — merge drops text outside token spans at segment boundaries
**Where:** `backend/asr/providers/local/merge.py:311-312` (`start_char = spans[first][0]`; `end_char = _trailing_end(...)`) and `:231-239` (`_trailing_end` extends only over Unicode `P*` and a few closing brackets).

**What happens:** Each group's text is sliced from its first token's span start to its last token's end, extended only over punctuation. Two kinds of character are never emitted:
- **Text before the first token of a chunk.** `《三体》很好看` → `三体》很好看`.
- **Non-`P` characters the aligner drops (`_aligner_kept` is False) when they sit at a group boundary.** `+ = < > ~ ^ $ ¥ € ° ℃ © ×` and emoji are categories Sm, Sc, Sk and So.

Traced example: text `我用C++。你呢` with tokens `我 用 C 你 呢` and a speaker change at `你`. `snap_changes` actively snaps to this boundary, because the gap `++。` contains `。`. The output is `我用C` / `你呢`, and `++。` is gone. `$100` at the start of a sentence after `。` loses its `$`.

The same gap hides sentence ends: `_sentence_end` only looks at the `P`-extended tail, so `C++。` and `30℃。` never split a sentence.

**Why it matters:** These segments become the immutable `original` (D41), so revert cannot bring the text back. Existing tests only use pure CJK or `P`-only gaps (`test_build_segments_*`, `test_token_spans_never_skip_whitespace_or_kept_characters` maps `C++` but never splits there).

**Fix:**
1. In `_trailing_end`, also step over any character that is not whitespace and not `_aligner_kept` (`cat[0] in "PS"`, or simply `not ch.isspace() and not _aligner_kept(ch)`).
2. In `build_segments`, track a per-chunk `cursor`: each group starts at `cursor` (initially 0) instead of `spans[first][0]`, and the last emitted segment absorbs any non-blank remainder after the final sentence-end emit.
3. Add regression tests for `《…》` at chunk start, `C++。` plus a speaker change, `30℃。`, and `$100`.

### m1 — Minor — cloud per_channel never diarizes channel 0 (D36 / PLAN §6.3)
**Where:** `dashscope_filetrans.py:300` (`diarize = not prepared.per_channel and …`), `:403-404`; `normalize.py:146-148` (one speaker per channel).

**What happens:** PLAN §4.5 [0b] documents "cloud: channel_id=[0,1] without diarization". But PLAN §6.3 and D36 promise that remote participants on ch 0 get diarization for desktop recordings, and cloud is the default provider (D5). Every remote participant on a desktop-recorded cloud job collapses into one `SPEAKER_00`, and no warning is added.

**Fix:** The smallest change is to append a warning when `per_channel` and `num_speakers != 2`. The complete fix is to extract ch 0 to mono FLAC and submit it with diarization, submit ch 1+ without it, and merge the two results.

### m2 — Minor — URL sources enable diarization on possibly stereo audio
**Where:** `dashscope_filetrans.py:343-351` with `:300`.

**What happens:** For path sources, D20 extracts the selected channel to mono precisely because "DashScope diarizes mono audio only". URL sources cannot be transcoded (D3), yet `right` sends `channel_id=[1]` with `diarization_enabled=true`, and so do `auto` / `left`. On a stereo URL this asks DashScope for exactly what D20 says it does not support.

**Fix:** For URL sources with `right`, disable diarization or add a warning. Document in D3/D20 that diarization on URL jobs assumes mono audio.

### m3 — Minor — cloud language hints are not normalised
**Where:** `dashscope_filetrans.py:166` vs `local/pipeline.py:352-372`.

**What happens:** The local provider maps `zh-CN` / `zh_Hans` / `Chinese` to `zh` and treats `auto` and unknown codes as no hint. The cloud body sends the raw strings, including `"auto"`. I did not verify whether DashScope rejects these, but the same `JobOptions` means different things per provider, which goes against the spirit of rule 4.

**Fix:** Move `normalize_language` into a Web-free shared module (e.g. `asr/providers/languages.py`). In `build_request_body`, keep only the non-`None` normalised codes.

### m4 — Minor — a short polling outage fails a long cloud job
**Where:** `client.py:163` (after `max_retries=5`, roughly 1+2+4+8+16 s), `dashscope_filetrans.py:530`.

**What happens:** `get_task` raises `ProviderError` once its retries run out, and `_wait` does not catch it. A job allowed to run for hours (`timeout_s` = 6 h) is lost to a network drop of about 30 s (laptop sleep, Wi-Fi). The remote task keeps running and is billed, and retry resets `cloud_task_id` (`server/jobs.py:242`), so the audio is transcribed again. `test_poll_retries_exhausted` pins the current behaviour.

**Fix:** In `_wait`, treat transport errors and 429/5xx from `get_task` as "keep polling" (log, capped backoff) until `deadline`. Fail immediately only on 4xx, `FAILED` and `UNKNOWN`.

### m5 — Minor — undocumented 20-window cluster floor
**Where:** `diar_campplus.py:40, 62` (`threshold = max(0.03 * N, 20)`).

**What happens:** D34 says clusters under 3 % of the windows are folded. With windows every 0.75 s, the floor of 20 dominates whenever N < 667 windows (about 8 min of speech). In a 3-minute call (≈ 240 windows), a speaker with 10 s of speech (≈ 13 windows) is folded into someone else, even though 3 % would be 7 windows. `test_fold_small_clusters` only covers N = 203 with 100 / 100 / 3 windows.

**Fix:** Either document the floor in D34 and add a short-clip test, or scale it, e.g. `max(min_share * N, min(min_windows, 0.05 * N))`.

### m6 — Minor — long VAD segments are cut at arbitrary points
**Where:** `vad.py:25-30`.

**What happens:** Any VAD segment longer than `max_len` (25 s) is split into equal pieces. The cuts land wherever the arithmetic says, usually mid-syllable, with no overlap. Each chunk is recognised and aligned on its own, so words at the seam are lost or duplicated. Long continuous turns are common in meetings. The bake-off (D34) used the same chunks, but the risk is not tested or documented.

**Fix:** Pass the audio into `merge_segments`, or post-process the spans, and move each cut to the lowest-energy 10 ms frame within about ±2 s of the even boundary.

### m7 — Minor — the LLM "writes a term" exemption is unbounded
**Where:** `correct/llm.py:58-61`.

**What happens:** `_writes_term` checks only `after`, so a correction replacing a 40-character `before` with `Townfall` is exempt from `MAX_EDIT_RATIO`. A hallucinated rewrite that turns a whole clause into a glossary term passes the guard that D44 relies on. Impact is limited because suggestions are never auto-applied.

**Fix:** Also require that `before` is short, e.g. `len(before.strip()) <= max(2 * len(name), len(name) + 4)`. Ideally also check pinyin or edit similarity, as L1 does.

### n1 — Nit — fold asymmetry in `token_spans`
**Where:** `merge.py:101` vs `:105`.

**What happens:** The text keeps a character whose NFKC/lower form has more than one character (`½`, `ﬁ`, `İ`, `Ⅻ`), but the needle always folds it. A token containing one never matches, so `token_spans` returns `None` and the whole chunk (up to 25 s, possibly several speakers) becomes one segment with one speaker.

**Fix:** Build the needle with the same rule: `_fold(c) if len(_fold(c)) == 1 else c`.

### n2 — Nit — `httpx.Client` never closed
**Where:** `llm.py:219`, `summarize.py:184`.

**What happens:** When no `http` is passed, both classes create a client and never close it. The server routes pass their own (`server/routes/transcripts.py:169, 238`); `asr summarize` in the CLI (`cli.py:487`) does not.

**Fix:** Add `close()` / `__enter__` / `__exit__`, or use a `with httpx.Client(...)` in the CLI.

### n3 — Nit — two model sets in memory during a swap
**Where:** `pipeline.py:145`.

**What happens:** `_ENGINE_CACHE.clear()` runs after the new set has loaded, so the old ASR, aligner and CAM++ stay resident during the load.

**Fix:** Clear the cache before building the new set (it is already under `_ENGINE_LOCK`).

### n4 — Nit — misleading 403 on result download
**Where:** `client.py:123`.

**What happens:** `download_json` goes through `raise_for_response`, which maps any 401/403 to `AUTH_MESSAGE`. An expired or forbidden signed OSS URL then tells the user to check their API key.

**Fix:** Add an `auth_errors: bool = True` parameter to `request` / `raise_for_response`, and pass `False` from `download_json`.

### n5 — Nit — speaker numbering differs per provider
**Where:** `pipeline.py:235-238`, `normalize.py:146-148`.

**What happens:** Locally, a silent channel 0 still takes `SPEAKER_00`, so the transcript shows only `SPEAKER_01`. Cloud numbers only the channels it got sentences for. IDs are opaque, but the two providers number differently.

**Fix:** Allocate ids lazily, when a track first emits a segment.

### n6 — Nit — stale `words` after [5b] corrections
**Where:** `correct/__init__.py:181`.

**What happens:** `model_copy(update={"text": text})` keeps the raw `words`. After a collapse or term replacement they no longer map onto the text, so `editing._split_point` silently falls back to interpolation.

**Fix:** Either document this, or drop or trim the `words` covering each collapsed span.

## Test coverage gaps
- **Merge:** text before the first token; symbols (`+ $ ℃`) at speaker, pause and sentence boundaries (M1); folding of non-ASCII compatibility characters (n1).
- **Diarization:** `fold_small_clusters` with small N, where the floor dominates (m5).
- **VAD:** `merge_segments` with a 60 s segment, checking where the cuts land (m6).
- **Cloud:**
  - language hints `auto` / `zh-CN`;
  - URL + `right` + diarization;
  - a polling outage longer than the retry budget;
  - a 403 on the result URL;
  - per_channel with more than 2 channels (`channel_id=[0..n-1]`, billed per channel).
- **LLM guard:** a long `before` replaced by a term (m7).
- **Real models:** CAM++ / aligner / FSMN behaviour is covered only by fakes. Real-audio tests skip without fixtures (D10), so funasr clustering edge cases (very few windows, `oracle_num` larger than the number of windows) are untested.

## Commands for the supervisor (not run here)
```
cd backend
uv run pytest tests/test_local_pipeline.py tests/test_provider_contract.py tests/test_cloud_provider.py tests/test_cloud_units.py tests/test_correct.py tests/test_editing.py tests/test_writers.py tests/test_llm_correct.py tests/test_summarize.py tests/test_audio_decode.py tests/test_audio_transcode.py tests/test_audio_channels.py
uv run pytest   # full suite before merge
```

## Merge verdict
**OK with notes.** No Critical issues and no hard-rule violations. M1 should be fixed (with regression tests) before release; m1–m7 should be fixed or documented in EXECUTION_LOG §4.