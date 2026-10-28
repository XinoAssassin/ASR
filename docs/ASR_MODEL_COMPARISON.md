# Open-Source ASR Model Comparison (mid-2026)

Scenario: Chinese-primary speech with embedded English technical terms; offline/local inference; dev machine is an AMD CPU without CUDA, NVIDIA GPU available later; word/character-level timestamps required so transcripts can be merged with speaker diarization output.

## 1. Candidates

| Model | Released | Params | Architecture | Avg CER on 4 Mandarin sets¹ | Code-switching | Hotwords / context | Timestamps | CPU viability | License |
|---|---|---|---|---|---|---|---|---|---|
| **Qwen3-ASR-1.7B / 0.6B** | 2026-01 (native transformers 2026-06) | 1.7B / 0.6B | Encoder + Qwen LLM decoder | 3.76 | Yes (30 languages + 22 dialects) | `context=` free text, up to 10k tokens | Qwen3-ForcedAligner-0.6B (NAR, segments up to 5 min) | 0.6B usable; 1.7B slow | Apache 2.0 |
| **FireRedASR2-AED / -LLM** | 2026-02 | 1.1B / 8B | AED / Encoder-Adapter-LLM | **3.05 / 2.89** (current SOTA) | Yes | Not provided | AED emits character-level timestamps + confidence | AED marginal; LLM needs GPU | Apache 2.0 |
| **Fun-ASR-Nano-2512** | 2025-12 | ~0.8B | Encoder + Qwen-0.6B decoder | 4.55 | Yes (zh/en/ja + dialects) | Native `hotwords=[...]` | Via funasr VAD segments | Good: OpenVINO / GGUF paths | Apache 2.0 |
| **SenseVoice-Small** | 2024-07 | 234M | Non-autoregressive | ~5 (Large: 4.47) | Yes | None | No character-level; VAD segments only | Excellent, 10x+ faster than Whisper | MIT |
| **SeACo-Paraformer-large** | 2023 | 220M | Non-autoregressive CIF | 4.56 | Weak (Chinese-centric training) | Native hotwords | Character-level | Excellent | Apache 2.0 |
| **Whisper large-v3 / turbo** | 2023–24 | 1.6B / 0.8B | Autoregressive AED | 9.86 (meeting set 18.9) | Drops/hallucinates at switch points | Weak `initial_prompt` | Needs WhisperX alignment | int8 usable but slow | MIT |

¹ Average over aishell1 / aishell2 / WenetSpeech-net / WenetSpeech-meeting, taken from each project's README (FireRedASR2S and Qwen3-ASR self-reported).

Key individual sets (CER %, lower is better):

| Test set | FireRedASR2-LLM | FireRedASR2-AED | Qwen3-ASR-1.7B | Fun-ASR | Paraformer-L | Whisper-v3 |
|---|---|---|---|---|---|---|
| WenetSpeech-meeting (closest to our use case) | 4.32 | 4.53 | 5.88 | 5.78 | 6.97 | 18.87 |
| WenetSpeech-net | 4.44 | 4.57 | 4.97 | 6.85 | 6.74 | 10.48 |
| aishell2 | 2.15 | 2.51 | 2.71 | 2.38 | 2.85 | 4.96 |

## 2. Assessment

**Qwen3-ASR**
- Pros: second-tier accuracy with a small gap to SOTA; `context` injection of a term list directly addresses the English-terminology pain point; official ForcedAligner for timestamps; single model handles offline and streaming, audio up to 20 min; native transformers support with a clean dependency tree; 0.6B runs on CPU.
- Cons: autoregressive LLM decoding, so 1.7B is noticeably slower on CPU than NAR models; known issue #140 (streaming mode repeats hotwords when audio sounds similar); requires Python 3.12.

**FireRedASR2S**
- Pros: lowest CER on Mandarin and meeting sets; AED variant emits character-level timestamps and confidence; bundled VAD/LID/Punc modules with leading metrics.
- Cons: no hotword/context interface, so English terms can only be fixed in post-processing; AED limited to 60 s per segment, LLM to 30 s, so VAD chunking is mandatory; scripts are Linux-oriented and need Windows adaptation; the 8B LLM variant will not run on the dev machine.

**Fun-ASR-Nano-2512**
- Pros: one-line integration through the funasr SDK; VAD, punctuation and CAM++ diarization live in the same framework, so engineering cost is lowest; native `hotwords`; OpenVINO and GGUF CPU acceleration paths.
- Cons: roughly 1 point behind Qwen3-ASR / FireRedASR2 on average; on the meeting set (5.78) it is on par with Qwen3.

**SenseVoice-Small / Paraformer**
- Pros: fastest on CPU; suited to long-audio batch and real-time.
- Cons: SenseVoice has no hotwords and no character timestamps; Paraformer is an older model with weak English-term recognition. Both are kept as a "fast mode" fallback.

**Whisper**
- CER on the Chinese meeting set is close to 19%, 3–4x the domestic models, with frequent hallucinations at language switch points. Great ecosystem, but not suitable as the primary model here. Kept only for English-only audio.

## 3. Recommendation

**Primary backend: Qwen3-ASR** (1.7B on CUDA, 0.6B on the CPU dev machine)
- Rationale: satisfies accuracy, hotword/context, timestamps, streaming, license and maintenance activity; it is the only candidate that ships both context injection and an official aligner, which map exactly onto this project's two core needs (English terms + speaker merge).

**Fallback backend: Fun-ASR-Nano-2512 (funasr SDK)**
- Rationale: faster on CPU, native hotwords, same framework as CAM++ diarization; cheap to switch to if Qwen3-ASR-0.6B is too slow on the dev machine.

**Reference: FireRedASR2-AED**
- Add once an NVIDIA GPU is available to measure the accuracy ceiling; no hotword interface, so not the default.

**Dropped: Whisper as the Chinese primary model.** faster-whisper is retained only as an English-only option.

## 4. How the final decision is made

Public benchmarks differ by only 1–2 points; the real decision comes from your own audio. Milestone M4 runs a controlled test:

1. Take 3–5 minutes of representative recordings (with English terms) and hand-correct a reference transcript.
2. Run Qwen3-ASR-0.6B / 1.7B, Fun-ASR-Nano and SeACo-Paraformer with the same term list.
3. Record CER, English-term recall and CPU real-time factor (RTF).
4. Pick the default backend from the results; keep the others as `--backend` options.

## 5. Sources

- Qwen3-ASR README (Evaluation table, updated 2026-06): github.com/QwenLM/Qwen3-ASR
- FireRedASR2S README (24-test-set comparison): github.com/FireRedTeam/FireRedASR2S
- FireRedASR README (Paraformer / SenseVoice / Whisper baselines): github.com/FireRedTeam/FireRedASR
- Qwen3-ASR issues #106 / #140 / #157 (context hotword usage and known issues)
- funasr.com model selection guide and Fun-ASR-Nano guide (hotwords, CPU paths)
