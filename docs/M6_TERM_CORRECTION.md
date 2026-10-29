# M6 — Term sets both ways and [5b] post-correction

Date: 2026-10-29. Same 4-minute excerpt, subtitle reference, 17 term occurrences and metrics as
`docs/M4_BAKEOFF.md`. Reproduce with `backend/scripts/correction_eval.py`; aliases used for the
"learning loop" rows: `tests/fixtures/4people_aliases.txt`.

## 1. Decoding-time terms (L0)

| System | Without terms: lenient CER / term hits | With the term list |
|---|---|---|
| Local Qwen3-ASR-1.7B (bf16), terms as system context | 9.42 % / 10 | **8.64 % / 15** |
| Local Qwen3-ASR-0.6B (bf16) | 11.15 % / 9 | 11.15 % / 14 |
| Cloud `qwen-audio-3.1-asr-flash-filetrans`, terms as `vocabulary` | 11.58 % / 10 ¹ | **9.33 % / 13** |
| SeACo-Paraformer, hotwords | 12.62 % / 7 | 12.71 % / 8 |

¹ The run without vocabulary transcribed the whole 31-minute file and was scored on the excerpt;
the run with vocabulary transcribed the excerpt only. The term gain is clear, part of the CER gain
may be segmentation.

## 2. L1 term correction (asr-hotword phoneme matching)

Lenient CER / term hits, applied to saved outputs. "Aliases" = mis-hearings recorded from this
same clip, so those rows are **optimistic**.

| Hypothesis | Raw | L1 terms @0.85 | L1 terms @0.80 | L1 + aliases @0.85 | L1 + aliases @0.80 |
|---|---|---|---|---|---|
| Local Qwen3-1.7B + terms | 8.64 % / 15 | 8.64 % / 15 | 8.82 % / 15 | **8.47 % / 16** | 10.20 % / 16 |
| Local Qwen3-1.7B, no terms | 9.42 % / 10 | 9.33 % / 11 | 9.51 % / 11 | 8.82 % / 14 | 10.54 % / 14 |
| Cloud filetrans, no vocabulary | 11.58 % / 10 | 11.50 % / 11 | 11.67 % / 12 | 10.89 % / 15 | 12.53 % / 16 |
| Cloud `qwen-audio-3.1-asr-flash` | 8.21 % / 11 | 8.12 % / 12 | 8.30 % / 12 | **7.43 % / 16** | 9.08 % / 16 |
| SeACo-Paraformer | 12.62 % / 7 | 12.45 % / 9 | 12.45 % / 10 | 11.06 % / 16 | 12.45 % / 16 |

Replacements per 4 minutes: 0–8 at 0.85 without aliases, 1–15 with aliases at 0.85, 16–24 with
aliases at 0.80 (where CER rises: false positives).

**Precision outside the scored window** (full 31-minute local transcript, threshold 0.85):
terms only → 5 replacements, all correct (结巴 → 街霸 ×4, 混游 → 魂游); terms + aliases → 12
replacements, 11 correct; the miss is 那接下来 → "Nadya下来" via the alias 娜姐, the case the
blacklist exists for (`Nadya | 娜姐 ~~~ 下来`).

## 3. Decisions

- Threshold default **0.85** (asr-hotword's default): no false positive seen without aliases; 0.80
  over-corrects as soon as aliases are present.
- L1 and the repetition filter are **on by default** for every provider; both toggles and the
  threshold are per job (`options.correction`).
- Aliases give the large gains; the review UI (M7) should make "add as alias" one click, and
  aliases that are common words need a blacklist.
- The repetition filter changed nothing on these clips (no Qwen3 #140-style runs occurred); it is
  kept as a guard and is unit-tested.
