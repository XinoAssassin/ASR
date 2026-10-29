# M4 — Local ASR bake-off (with cloud baselines)

Date: 2026-10-29. Decision at the end (§5). Raw outputs stay in the git-ignored `backend/data/bakeoff/`.

## 1. Setup

| Item | Value |
|---|---|
| Audio | `ASR_4people-test.m4a` (Bilibili gaming podcast EP.78, 4 speakers, 48 kHz dual-mono AAC), excerpt **870.5–1105.0 s = 234.5 s** |
| Reference | The video's Bilibili CC subtitles (`tests/fixtures/4people.ass`, git-ignored), 86 cues fully inside the window, 1 186 tokens. Subtitle offset auto-estimated by VAD overlap: −0.15 s (99 % of cue time covered by speech) |
| Terms | `tests/fixtures/4people_terms.txt`; 10 of them occur in the window, 17 occurrences: Nadya, Ryoma, Townfall ×2, jump scare, 寂静岭 ×4, 生化危机, 街霸 ×3, 死或生 ×2, 魂游, 2D |
| Segmentation | One FSMN-VAD pass, segments merged into 12 chunks of ≤ 25 s. **Every model, local and cloud, gets the same 12 chunks** (the filetrans baseline excepted, see §4) |
| Hardware | AMD Ryzen AI MAX+ 392 (Zen 5, AVX-512 incl. BF16/VNNI), WSL2, 31 GB; torch 2.14.1+cpu, dispatch `AVX512`, 16 threads |
| Software | transformers 5.21 (native Qwen3-ASR and Fun-ASR-Nano), funasr 1.4.17 (SeACo-Paraformer, FSMN-VAD); language forced to Chinese |
| Metric | Mixed error rate: CJK characters + Latin words, NFKC, punctuation dropped (`asr/eval/metrics.py`). **Lenient** additionally drops interjection fillers (嗯啊呃哦…) on both sides, because the subtitles omit them while faithful models transcribe them; it is the primary metric. RTF = processing time ÷ audio time (local: model only, after warm-up; cloud: wall clock incl. network) |

Reproduce:

```
cd backend
uv sync --extra cpu --extra local
ASR_MODEL_CACHE_DIR=$PWD/models uv run --no-sync asr models download fsmn-vad seaco-paraformer qwen3-asr-0.6b fun-asr-nano
ASR_MODEL_CACHE_DIR=$PWD/models uv run --no-sync asr models download qwen3-asr-1.7b --mirror
ASR_MODEL_CACHE_DIR=$PWD/models uv run --no-sync python scripts/bakeoff.py \
    --audio ../ASR_4people-test.m4a --subs ../tests/fixtures/4people.ass \
    --start 870 --end 1110 --terms-file ../tests/fixtures/4people_terms.txt --out data/bakeoff/run
uv run --no-sync python scripts/cloud_flash_eval.py --run data/bakeoff/run      # costs money
uv run --no-sync python scripts/cloud_realtime_eval.py --run data/bakeoff/run   # costs money
uv run --no-sync python scripts/bakeoff_table.py data/bakeoff/run
```

## 2. Results (sorted by lenient CER)

| # | Model | Where | dtype | Terms in | CER raw | CER lenient | S / D / I | Term hits | RTF | Peak RSS |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `qwen-audio-3.1-asr-flash` | cloud | - | no | 11.20% | **8.21%** | 34 / 9 / 87 | 11/17 | 0.20 (wall) | - |
| 2 | `qwen-audio-3.1-asr-flash` | cloud | - | yes | 11.46% | **8.47%** | 40 / 9 / 84 | 11/17 | 0.36 (wall) | - |
| 3 | `qwen3-asr-1.7b` | local | float32 | yes | 11.46% | **8.47%** | 31 / 18 / 84 | 15/17 | 1.449 | 12.0 GB |
| 4 | `qwen3-asr-1.7b` | local | bfloat16 | yes | 11.63% | **8.64%** | 32 / 18 / 85 | 15/17 | 0.432 | 4.7 GB |
| 5 | `qwen3-asr-flash` | cloud | - | yes | 11.54% | **8.73%** | 35 / 16 / 83 | 14/17 | 0.21 (wall) | - |
| 6 | `qwen3-asr-flash` | cloud | - | no | 12.06% | **9.25%** | 38 / 15 / 87 | 11/17 | 0.16 (wall) | - |
| 7 | `fun-asr-flash-2026-06-15` | cloud | - | no | 12.06% | **9.25%** | 38 / 20 / 82 | 12/17 | 0.16 (wall) | - |
| 8 | `qwen3-asr-1.7b` | local | float32 | no | 12.32% | **9.33%** | 37 / 17 / 89 | 10/17 | 1.411 | 12.0 GB |
| 9 | `qwen3-asr-1.7b` | local | bfloat16 | no | 12.40% | **9.42%** | 36 / 17 / 91 | 10/17 | 0.437 | 4.7 GB |
| 10 | `qwen-audio-3.1-asr-flash-message` | cloud | - | no | 11.63% | **9.59%** | 38 / 29 / 68 | 11/17 | 0.79 (wall) | - |
| 11 | `qwen-audio-3.0-asr-flash` | cloud | - | no | 11.89% | **9.59%** | 41 / 19 / 78 | 11/17 | 0.53 (wall) | - |
| 12 | `qwen-audio-3.1-realtime-plus` | cloud | - | no | 12.23% | **9.59%** | 40 / 19 / 83 | 11/17 | 0.83 (wall) | - |
| 13 | `qwen3-asr-0.6b` | local | float32 | yes | 13.78% | **11.15%** | 47 / 22 / 91 | 14/17 | 0.594 | 4.9 GB |
| 14 | `qwen3-asr-0.6b` | local | bfloat16 | yes | 13.78% | **11.15%** | 45 / 24 / 91 | 14/17 | 0.206 | 2.3 GB |
| 15 | `qwen3-asr-0.6b` | local | bfloat16 | no | 13.95% | **11.15%** | 57 / 22 / 83 | 9/17 | 0.198 | 2.3 GB |
| 16 | `qwen3-asr-0.6b` | local | float32 | no | 14.13% | **11.32%** | 58 / 23 / 83 | 8/17 | 0.598 | 4.9 GB |
| 17 | `fun-asr-nano` | local | float32 | no | 14.56% | **11.50%** | 54 / 19 / 96 | 11/17 | 0.589 | 5.4 GB |
| 18 | `qwen-audio-3.1-asr-flash-filetrans` | cloud | - | no | 13.44% | **11.58%** | 38 / 47 / 71 | 10/17 | - | - |
| 19 | `fun-asr-nano` | local | bfloat16 | yes | 14.13% | **12.10%** | 58 / 29 / 77 | 12/17 | 0.201 | 2.6 GB |
| 20 | `fun-asr-nano` | local | float32 | yes | 14.38% | **12.10%** | 58 / 29 / 80 | 12/17 | 0.580 | 5.4 GB |
| 21 | `fun-asr-nano` | local | bfloat16 | no | 14.99% | **12.27%** | 61 / 25 / 88 | 8/17 | 0.204 | 2.6 GB |
| 22 | `seaco-paraformer` | local | float32 | no | 16.11% | **12.62%** | 78 / 11 / 98 | 7/17 | 0.033 | 2.6 GB |
| 23 | `seaco-paraformer` | local | float32 | yes | 16.19% | **12.71%** | 79 / 11 / 98 | 8/17 | 0.034 | 2.6 GB |
| 24 | `fun-asr-flash-8k-realtime` | cloud | - | no | 17.40% | **14.69%** | 74 / 31 / 97 | 8/17 | 0.76 (wall) | - |

"Terms in" = term list given to the model (Qwen3: system prompt; Fun-ASR-Nano: `keywords`; Paraformer: `hotword`; cloud: see §4).

### Term recall per term (local, with terms)

| Term (occurrences) | Paraformer | Fun-ASR-Nano | Qwen3-0.6B | Qwen3-1.7B |
|---|---|---|---|---|
| 寂静岭 (4) | 1 | 3 | 4 | 4 |
| 街霸 (3) | 3 | 3 | 3 | 3 |
| Townfall (2) | 0 | 0 | 1 | **2** |
| 死或生 (2) | 1 | 2 | 2 | 2 |
| Nadya (1) | 0 | 0 | 1 | 1 |
| jump scare / 生化危机 / 魂游 (1 each) | 3 | 3 | 3 | 3 |
| 2D (1) | 0 | 1 | 0 | 0 |
| Ryoma (1) | 0 | 0 | 0 | 0 |
| **Total (17)** | 8 | 12 | 14 | **15** |

Every model hears "Ryoma" as 龙马; Paraformer writes 寂静岭 as 基金岭 / 金银脸 and Townfall as 汤们OP / tomford.

## 3. Findings

1. **Qwen3-ASR-1.7B is the most accurate local model** and matches the best cloud model (8.47–8.64 % vs 8.21 %) with the **best term recall of all 24 runs (15/17)**.
2. **bf16 makes 1.7B practical on CPU.** On AVX512_BF16 hardware it is 3.3× faster than fp32 (RTF 1.45 → 0.43) at 40 % of the memory (12.0 → 4.7 GB) for +0.17 points CER. Qwen3-0.6B shows no accuracy loss in bf16 at all. Without native BF16, 1.7B is slower than real time.
3. **The term list matters for the Qwen3 family**: 1.7B 10 → 15 hits and −0.8 to −0.9 points CER; 0.6B 8–9 → 14 hits. Paraformer hotwords (7 → 8) and Fun-ASR-Nano keywords (8–11 → 12) help much less.
4. **Fun-ASR-Nano is dominated by Qwen3-0.6B** (same speed and memory in bf16, ~1 point worse, fewer terms) and is no longer the planned fallback.
5. **SeACo-Paraformer is 6× faster than anything else** (RTF 0.033, ~30× real time) at +1.5 points CER vs 0.6B and poor English-term handling; useful only as a "fast draft" mode. It must stay fp32: bf16 breaks its CIF timestamp predictor.
6. **Cloud.** The short-audio HTTP models (`qwen-audio-3.1-asr-flash`, `qwen3-asr-flash`, `fun-asr-flash`) beat the async filetrans model we use today (8.2–9.3 % vs 11.6 %). Only `qwen3-asr-flash` honours an inline term list (11 → 14 hits); the qwen-audio-3.x / fun-asr-flash models accept `vocabulary` / context fields but ignore them (they need a pre-registered `vocabulary_id`). `-message` and `realtime-plus` are WebSocket models with similar accuracy (9.6 %); the 8 kHz telephony model is clearly worse on wide-band audio (14.7 %).

## 4. Caveats

- One 4-minute excerpt of one recording (casual gaming podcast, fast turn-taking, many fillers). Differences below ~1 point are within noise; the ranking of families is the robust result, not the exact digits.
- The reference is edited subtitles, not a verbatim transcript; even lenient CER keeps ~8 % of style differences (得/的, dropped repetitions, rephrasing). It biases all models equally.
- 17 term occurrences is a small sample; the user's real domain (chip / technical English terms) still needs its own clip before M6's "visible term-recall gain".
- The filetrans baseline ran on the whole 31-minute file with diarization and its sentences were attributed to the window by midpoint, so its segmentation differs from the chunked runs; its higher deletion count (47) is partly boundary loss.
- Local RTFs were measured on WSL2 with other light load (model downloads during part 1). Cloud RTFs are wall-clock through a proxy and say little about server speed.

## 5. Decision

| Role | Model | When |
|---|---|---|
| **Local default** | `qwen3-asr-1.7b`, bf16 | CUDA, or CPU with native BF16 (`resolve_dtype("auto") == "bfloat16"`) |
| Local default (fallback hardware) | `qwen3-asr-0.6b` | CPU without native BF16 (1.7B fp32 is slower than real time) |
| Fast draft mode | `seaco-paraformer` (fp32) | very long batches, previews |
| Kept as option, not default | `fun-asr-nano` | — |
| Term list | Always passed to Qwen3 as system-prompt context | — |

Follow-ups (not M4): evaluate a "cloud chunked" mode (local VAD + `qwen3-asr-flash` with terms, local CAM++ diarization) against the current filetrans provider after M5; collect a technical-terms clip for M6.
