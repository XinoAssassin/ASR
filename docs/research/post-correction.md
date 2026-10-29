# Research: How similar projects correct ASR output (post-correction)

Context: PLAN §4.3 has no dedicated correction stage. Term injection happens before decoding (`context` / `hotwords` / `vocabulary`), a manual "replace by term set" is planned for M7, and "LLM correction" is parked in M11. This note surveys what comparable open-source projects actually do, so a correction stage can be designed from precedent rather than from scratch.

## 1. Survey

### 1.1 Transcription apps built on Whisper/WhisperX (Buzz, WhisperX, whisper-diarization, Whisper-WebUI, noScribe)

- None ships an automatic post-correction stage or a dictionary replace engine. All of them expose Whisper's `initial_prompt` (~200 tokens) as the only vocabulary mechanism, i.e. pre-decoding biasing.
- noScribe deliberately stops at a human editor UI (offline / GDPR research use).
- Commercial Whisper front-ends (MacWhisper Pro, superwhisper, Handy) add an optional "AI post-processing" step: the whole transcript is sent to a local or cloud LLM with a user-editable prompt. No diff review, no term-level audit trail.

Takeaway: the mature *multi-speaker transcript* tools do not solve this; the dictation tools do, but crudely.

### 1.2 Phoneme-level hotword replacement — `HaujetZhao/asr-hotword` (extracted from CapsWriter-Offline)

Directly verified from the README (fetched 2026-10).

- Converts both the recognised text and each hotword to a phoneme sequence: Chinese → pinyin initials / finals / tone with similarity classes (front/back nasal, flat/retroflex), English → letters with LCS fuzzy match. Mixed zh/en supported.
- Two-stage retrieval (FastRAG + AccuRAG), replace when similarity ≥ threshold (default 0.85); candidates between `threshold-0.2` and `threshold` are reported but not applied.
- Hotword file syntax: `Target | alias1 | alias2 ~~~ blacklist1 | blacklist2`. Aliases capture known mis-hearings (`Claude | 克劳德 | cloud`, `Llama.cpp | 拉马点 CPP`); the blacklist blocks a replacement when a listed word appears within N neighbouring tokens (prevents `扶贫办` → `傅平办`).
- Performance: 5 000 hotwords, one sentence ≈ 20 ms; demo sentences 0.5–2 ms. Dependencies: `pypinyin`, `rapidfuzz` only.
- Returns `CorrectionResult.matches` (applied) and `.similars` (near misses) — exactly the audit data a review UI needs.

This is the closest match to our "English term heard as homophone Chinese" problem and runs on CPU in milliseconds.

### 1.3 General Chinese spelling correction — `shibing624/pycorrector`

- Two engines: KenLM n-gram + pinyin/shape rules (1–5 ms per sentence, no GPU) and MacBERT4CSC (50–200 ms per sentence on CPU, better recall on context-dependent homophones).
- Supports a custom confusion set (`wrong right` pairs, forced) and a custom word list with frequencies (whitelist to stop proper nouns being "corrected").
- Designed for input-method / ASR text; corrects generic homophone errors (`因该` → `应该`), not domain terms unless they are in the confusion set.

Takeaway: complementary to 1.2 — handles common-word homophones; the term layer handles domain vocabulary. MacBERT adds a model download and ~1.5 GB RAM; KenLM mode is cheap.

### 1.4 LLM generative error correction (GER)

- Research line (GenSEC @ SLT 2024, "LLM-based GER for rare words with synthetic data and phonetic context", arXiv 2505.17410 / 2506.11091): an LLM rewrites the ASR hypothesis given (a) the transcript, (b) optionally N-best hypotheses, (c) a glossary, and (d) phonetic cues (IPA / pinyin) for the glossary terms. Phonetic cues are what keeps rare-word correction from over-correcting.
- Practical pattern in dictation tools (openwhispr, FastWord): glossary in the prompt + instruction "fix misheard words only, do not rewrite", optionally an auto-learning dictionary that records user corrections and feeds them back as aliases.
- Known failure modes reported across these sources: hallucinated rewrites, dropped sentences, merged speakers when the whole transcript is sent at once; mitigations are per-segment batching, strict "keep segment IDs" output format, and a diff threshold.

### 1.5 Built-in mechanisms we already use

- FunASR SeACo-Paraformer hotword bias decoder, Qwen3-ASR `context`, DashScope `vocabulary` (weights 1–5 / 50): decoding-time biasing. Reported issue: Qwen3-ASR repeats hotwords on phonetically similar input (#140) — a post-filter for repetition is still needed.
- FunASR ships ITN (inverse text normalisation: numbers, dates) and punctuation as separate post-processing models; the cloud model does both server-side.

## 2. Pattern that emerges

| Layer | Who does it | Cost | Risk | Auditability |
|---|---|---|---|---|
| L0 decoding-time bias (hotwords / context / vocabulary) | FunASR, Qwen3-ASR, DashScope, Whisper `initial_prompt` | free | low | none (inside the model) |
| L1 phoneme-level term replace with aliases + blacklist | asr-hotword / CapsWriter | ms, CPU | low, deterministic | full (match list with scores) |
| L2 generic homophone CSC | pycorrector (KenLM or MacBERT) | ms – 200 ms/sentence | medium (false positives on names) | per-edit positions |
| L3 LLM GER with glossary + phonetic cues | research + dictation tools | seconds, money if cloud | high without guards | only via diff |
| L4 human review with term highlight | noScribe, our M7 | user time | — | — |

Mature projects either stop at L0 (Whisper tools) or go L0 + L1 (CapsWriter) and leave L3 as an opt-in.

## 3. Recommendation for this project

Add stage **[5b] Correct** after merge, applied identically to local and cloud transcripts:

1. **L1 term correction (default on)** — vendor or depend on `asr-hotword` (MIT-style, two deps). Extend `TermSet.terms` with `aliases: string[]` and `blacklist: string[]`; build the hotword text from all selected term sets. Store every applied match as `Correction {segment_id, start, end, before, after, score, source: "term"}`; keep `.similars` as suggestions for the UI.
2. **Repetition / hallucination filter (default on)** — drop or flag segments where a term or n-gram repeats > N times consecutively (Qwen3 #140 pattern) and segments with text but near-zero VAD energy.
3. **L2 generic CSC (off by default)** — `pycorrector` KenLM mode with the term set loaded as the custom word whitelist; MacBERT optional on GPU hosts. Surface only as suggestions, never auto-apply to segments containing a term match.
4. **L3 LLM pass (off by default, M7 with the review UI)** — per-speaker-turn batches of ≤ 20 segments, prompt = glossary with pinyin for each term + context text + rules (same segment IDs in / out, fix mis-hearings only, no additions or deletions). Reject any segment whose edit ratio > 15 %. Backend: local Qwen3 via the existing model cache, or DashScope `qwen-plus`. Output goes through the same `Correction` records with `source: "llm"`.
5. **Learning loop** — when the user accepts an LLM suggestion or hand-edits a term, offer "add as alias" to the term set, so L1 catches it next time (the openwhispr / FastWord pattern).

UI: corrections render as highlighted spans with before/after on hover; accept / reject per item, accept-all per source; `original` stays immutable so revert still works. Metrics for the M4 bake-off: term recall and CER before and after L1, on `ASR_4people-test.m4a`.

Milestone placement: L1 + repetition filter in M6 (with term-set wiring), L3 + review UI in M7, L2 as an M11 option.

## 4. Sources

- asr-hotword README (https://github.com/HaujetZhao/asr-hotword) — phoneme matching, alias/blacklist syntax, 20 ms per sentence, API. Direct read.
- CapsWriter-Offline (https://github.com/HaujetZhao/CapsWriter-Offline) — origin of the hotword engine.
- pycorrector (https://github.com/shibing624/pycorrector) — KenLM / MacBERT modes, custom confusion set and word dict; speed figures from a CSDN benchmark write-up, not independently measured.
- homophone-replacer (https://github.com/xinliu9451/homophone-replacer) — C++ lexicon + FST variant from sherpa-onnx.
- arXiv 2505.17410, 2506.11091 — LLM GER with phonetic context; GenSEC-LLM (https://huggingface.co/GenSEC-LLM).
- Buzz CLI docs, Whisper-WebUI advanced parameters, noScribe discussions #45 — `initial_prompt` only, no post-correction.
- openwhispr-custom, FastWord — dictation tools with glossary + LLM post-processing and auto-learning dictionaries (search summaries; not independently fetched).
