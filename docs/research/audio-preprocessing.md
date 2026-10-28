# Research: Audio preprocessing for the multi-speaker ASR pipeline (VAD -> Qwen3-ASR / DashScope -> CAM++ / pyannote)

Context: PLAN.md §4.3 step [0] currently reads "ffmpeg -> 16 kHz mono wav". This brief evaluates what else (if anything) belongs in that step, what should stay an optional toggle, and the CPU cost on the 16-core / 64 GB / no-CUDA dev box.

## Summary
Every mature reference project (WhisperX, whisper-diarization, FunASR, pyannote, NeMo) does exactly one mandatory thing: decode with ffmpeg to 16 kHz, mono, PCM16/float32. None of them apply loudness normalization, DC-offset removal, or de-clipping by default. Three independent 2022-2026 studies show that single-channel denoising/separation placed in front of a modern large-scale ASR model **raises** WER in every tested configuration, so denoising (DeepFilterNet, RNNoise, noisereduce, ClearerVoice, resemble-enhance) and demucs vocal separation must be **off by default** and exposed only as explicit, user-opted toggles for music/heavy-noise inputs. The recommended default chain is: ffmpeg decode -> channel policy (downmix, or per-channel for 2-channel telephony) -> 16 kHz mono float32 -> optional peak-safety gain -> VAD. Long files should be chunked at VAD silences for diarization with cross-chunk speaker reconciliation; the cloud path has an official 2-hour cap when diarization is on.

## Findings

### 1. What mature projects do by default

1. **Claim:** WhisperX's `load_audio` is a single ffmpeg call: `ffmpeg -nostdin -threads 0 -i FILE -f s16le -ac 1 -acodec pcm_s16le -ar 16000 -`, then int16 -> float32 / 32768. No normalization, filtering, or DC removal. **Sources:** [whisperx/audio.py](https://raw.githubusercontent.com/m-bain/whisperX/main/whisperx/audio.py). **Support:** direct evidence (source read). **Confidence:** high.
2. **Claim:** FunASR's loader (`funasr/utils/load_utils.py`) uses the identical ffmpeg command when ffmpeg is present; otherwise torchaudio/soundfile + `torchaudio.transforms.Resample`. Multi-channel is averaged (`mean(-1)` with `reduce_channels=True`) or, for 2-D numpy input, channel 0 is taken (`mat[:, 0]`). There is **no `channel_id` parameter** in this loader (contradicts an LLM search summary). **Sources:** [load_utils.py](https://raw.githubusercontent.com/modelscope/FunASR/main/funasr/utils/load_utils.py). **Support:** direct evidence. **Confidence:** high.
3. **Claim:** whisper-diarization (MahmoudAshraf97) runs `demucs.separate -n htdemucs --two-stems=vocals` **by default** and transcribes `vocals.wav`; the `--no-stem` flag's own help text says disabling it "helps with long files that don't contain a lot of music". Falls back to raw audio if demucs fails. **Sources:** [diarize.py](https://raw.githubusercontent.com/MahmoudAshraf97/whisper-diarization/main/diarize.py). **Support:** direct evidence. **Confidence:** high. *Researcher note:* this is the only reference project that pre-separates, and its target domain (podcasts/video with music) differs from meetings/interviews.
4. **Claim:** pyannote 3.1 and NeMo diarization expect 16 kHz mono; pyannote auto-resamples/downmixes when given a path, and NeMo docs/model cards instruct `ffmpeg -ac 1 -ar 16000`. **Sources:** [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1), [NeMo diarization datasets](https://docs.nvidia.com/nemo/speech/3.0.0/asr/speaker_diarization/datasets.html), [nvidia/diar_sortformer_4spk-v1](https://huggingface.co/nvidia/diar_sortformer_4spk-v1). **Support:** interpretation (search summaries; NeMo page fetched but the mono/16k statement sits in the model cards rather than the dataset page). **Confidence:** medium.
5. **Claim:** Qwen3-ASR accepts path / URL / base64 / `(np.ndarray, sr)` and resamples internally; its streaming demo resamples browser audio to 16,000 Hz; the ForcedAligner handles up to 5 minutes of speech per call. DashScope filetrans accepts "any sample rate" and mainstream formats, up to 12 h / 2 GB, and its own FAQ recommends `ffmpeg -i in -ac 1 -ar 16000 -sample_fmt s16 out.wav` as the canonical conversion. **Sources:** [QwenLM/Qwen3-ASR README](https://github.com/QwenLM/Qwen3-ASR), [Alibaba Cloud non-real-time ASR guide](https://www.alibabacloud.com/help/en/model-studio/non-realtime-speech-recognition-user-guide). **Support:** direct evidence. **Confidence:** high.
6. **Claim (loudness / DC / clipping):** No reference project applies loudness normalization, DC-offset removal, or de-clipping by default. Log-mel front-ends are largely gain-invariant, so loudness normalization is primarily a safety measure for extremely quiet input rather than an accuracy lever. **Sources:** items 1-2 above (absence in code). **Support:** researcher inference from code absence; the "gain-invariant" argument is reasoning, not a measured result. **Confidence:** medium. *Missing evidence:* no controlled study of ffmpeg `loudnorm` vs. raw audio on Qwen3-ASR/CAM++ was found.

### 2. Denoising / enhancement: does it help or hurt?

7. **Claim:** MetricGAN+ denoising ahead of Whisper, NVIDIA Parakeet, Gemini Flash 2.0 and Parrotlet degraded accuracy in **all 40 tested configurations** (4 models x 10 conditions); absolute semWER increase 1.1%-46.6%. Authors conclude modern ASR "possess sufficient internal noise robustness" and enhancement "may remove acoustic features critical for ASR". **Sources:** [arXiv:2512.17562 (Dec 2025)](https://arxiv.org/abs/2512.17562). **Support:** direct evidence (abstract fetched). **Confidence:** high for the finding; medium for generalization to Qwen3-ASR (not tested; researcher inference that a 2025 LLM-based ASR trained on large noisy data behaves like Whisper/Parakeet).
8. **Claim:** SAM-Audio separation before Whisper raised PSNR (cleaner signal) yet **increased WER and CER in every model-dataset configuration** (e.g. Whisper-base English WER 10.53% -> 21.66%). **Sources:** [arXiv:2603.04710 (2026)](https://arxiv.org/abs/2603.04710). **Support:** direct evidence. **Confidence:** high.
9. **Claim:** The mechanism is processing *artifacts* (SE error not expressible as speech+noise), identified as "the main cause of performance degradation"; the simple **observation-adding (OA)** trick (mix a scaled copy of the original back into the enhanced signal) monotonically raises signal-to-artifact ratio and recovers ASR accuracy. **Sources:** [Iwamoto et al., Interspeech 2022, arXiv:2201.06685](https://arxiv.org/abs/2201.06685). **Support:** direct evidence. **Confidence:** high. *Design implication (inference):* if a denoise toggle is offered, implement it as `out = (1-a)*enhanced + a*original` with a ~0.1-0.3 rather than pure enhanced output.
10. **Claim:** Tool-level characterization (RTF on CPU, artifact risk):
   - **DeepFilterNet2/3**: full-band 48 kHz model; paper reports RTF **0.04 on a notebook Core-i5** (single core). Must upsample 16 kHz input to 48 kHz, enhance, then downsample. Best quality/cost ratio among neural denoisers. **Sources:** [arXiv:2205.05474](https://arxiv.org/abs/2205.05474), [rikorose/DeepFilterNet](https://github.com/rikorose/deepfilternet). **Support:** direct (paper abstract) for RTF; interpretation for pipeline. **Confidence:** high (RTF), medium (fit).
   - **RNNoise**: tiny C GRU model, ~48 kHz frames, negligible CPU; weaker on non-stationary noise. **Support:** search summaries only. **Confidence:** medium.
   - **noisereduce**: spectral gating; cheap in batch but classic source of "musical noise" artifacts, the exact failure mode in finding 9. **Support:** interpretation. **Confidence:** medium.
   - **ClearerVoice-Studio** (`pip install clearvoice`): FRCRN_SE_16K (native 16 kHz, lighter), MossFormerGAN_SE_16K, MossFormer2_SE_48K; runs on CPU, NumPy-in/NumPy-out API added 2025-06; same Alibaba speech lab as FunASR. **Sources:** [modelscope/ClearerVoice-Studio](https://github.com/modelscope/ClearerVoice-Studio). **Support:** direct (README). **Confidence:** high for availability; **no CPU RTF figures published** in README.
   - **resemble-enhance**: denoiser UNet + CFM enhancer (32-64 ODE steps) + vocoder; GitHub issues report CPU far slower than real time; `--denoise_only` is the only CPU-viable mode. **Sources:** [resemble-enhance issue #39](https://github.com/resemble-ai/resemble-enhance/issues/39). **Support:** interpretation (issue summaries). **Confidence:** medium. *It is a generative model: it re-synthesizes speech and can alter timbre -> risk for speaker embeddings (inference).*
   - **demucs htdemucs**: whisper-diarization's choice. Blog benchmarks put CPU RTF at ~0.15-0.5 on fast desktop CPUs, RAM ~7-10 GB per hour of audio, with pre-splitting advised beyond 2 h. **Sources:** [facebookresearch/demucs](https://github.com/facebookresearch/demucs), third-party benchmark blogs. **Support:** interpretation; **no official CPU RTF**. **Confidence:** low.
   - **Dereverberation (WPE / nara_wpe)**: linear, artifact-light, classic front-end for far-field diarization; multi-channel WPE needs the raw channels (do it *before* downmix). **Sources:** [fgnt/nara_wpe](https://github.com/fgnt/nara_wpe). **Support:** interpretation. **Confidence:** medium. *The "10-25% DER reduction" figure in search summaries is unverified - treat as missing evidence.*
11. **Claim (diarization side):** No primary evidence was found that neural denoising improves CAM++ or pyannote speaker embeddings on meeting audio; the demucs step in whisper-diarization is motivated by music/background suppression, not speaker modelling. **Support:** absence of evidence. **Confidence:** low.

### 3. Channels, telephony, long files

12. **Claim:** For 2-channel call recordings (one speaker per channel), DashScope's `channel_id` accepts a list (e.g. `[0, 1]`), transcribes channels independently, and returns a `channel_id` per transcript; billing is per selected channel. Locally, FunASR has no channel selector, so the pipeline must split channels itself. **Sources:** [Alibaba Cloud guide](https://www.alibabacloud.com/help/en/model-studio/non-realtime-speech-recognition-user-guide), finding 2. **Support:** direct for the API; direct for FunASR. **Confidence:** high. *Inference:* per-channel ASR + VAD gives ground-truth speaker labels and should bypass diarization entirely; apply a per-channel energy gate to suppress cross-talk bleed.
13. **Claim:** For ordinary stereo (same mix on both channels, or a single mic recorded as stereo), downmixing via `-ac 1` (ffmpeg averages) is what every reference project does. *Inference:* detect the telephony case by measuring inter-channel correlation / per-channel activity ratio; auto-select per-channel mode only when channels are clearly independent, otherwise downmix. Expose a manual override (`channels: auto | downmix | left | right | per-channel`).
14. **Claim:** 8 kHz telephone audio should be upsampled to 16 kHz with ffmpeg's default (soxr/swr band-limited) resampler; upsampling adds no information but keeps all models in-spec. CAM++ `speech_campplus_sv_zh-cn_16k-common` is a 16 kHz model; FunASR also publishes 8 k variants of some models, and ClearerVoice trains separation at 8 k. **Support:** interpretation. **Confidence:** medium. *Missing evidence:* no measured CER/DER delta for 8 k-upsampled vs native 16 k on Qwen3-ASR/CAM++.
15. **Claim:** DashScope: when `diarization_enabled` is on, "keep the audio duration within 2 hours. Longer audio may cause recognition failures or timeouts." Without diarization the limit is 12 h / 2 GB. **Sources:** [Alibaba Cloud guide](https://www.alibabacloud.com/help/en/model-studio/non-realtime-speech-recognition-user-guide). **Support:** direct evidence. **Confidence:** high. *Implication:* the cloud provider must split >2 h files at VAD silences and re-label speakers across parts (or warn the user).
16. **Claim:** pyannote-style pipelines cluster all segment embeddings globally (agglomerative, O(N^2)), which becomes memory-heavy and unstable on multi-hour audio; community guidance in the pyannote tracker is to split long recordings into overlapping chunks, diarize locally, then reconcile speaker identities by centroid cosine similarity, plus post-merge / min-duration smoothing. **Sources:** [pyannote-audio#1966](https://github.com/pyannote/pyannote-audio/issues/1966). **Support:** interpretation (community reply, not maintainer doc). **Confidence:** medium. NeMo's own docs show training on 90-s windows of a 45-min AMI recording, i.e. windowed processing is standard. **Sources:** NeMo datasets page (direct). **Confidence:** high for NeMo convention.
17. **Claim:** Qwen3-ForcedAligner supports up to 5 minutes per call, so alignment must already be chunked; reuse the same VAD-derived chunk boundaries for ASR, alignment and diarization to keep timestamps consistent. **Sources:** Qwen3-ASR README (direct). **Confidence:** high (limit); inference (shared boundaries).

### 4. CPU cost on 16-core AMD, no CUDA (per hour of audio)

| Step | Cost estimate | Basis |
|---|---|---|
| ffmpeg decode + resample + downmix | seconds (RTF ~0.005) | ffmpeg is I/O-bound; direct experience, not benchmarked here |
| Peak/loudness gain (`loudnorm` 2-pass or `volume`) | seconds; 2-pass = 2 decodes | ffmpeg filter |
| FSMN-VAD | low (RTF << 0.1) | tiny model; inference |
| DeepFilterNet3 (16k->48k->16k) | ~2-5 min (RTF 0.04-0.1 single core; 16 cores help only via batching) | paper RTF on Core-i5 |
| RNNoise | < 1 min | C impl; summaries only |
| noisereduce | ~1 min batch | vectorized STFT; summaries only |
| ClearerVoice FRCRN_SE_16K | unmeasured; expected several min | no published CPU RTF |
| ClearerVoice MossFormer2_SE_48K | unmeasured; expected tens of min | transformer at 48 k; inference |
| demucs htdemucs (vocals) | ~10-30 min, 7-10 GB RAM | third-party blogs, low confidence |
| resemble-enhance (full) | hours; impractical | GitHub issues |
| resemble-enhance `--denoise_only` | unmeasured; tens of min likely | inference |
| nara_wpe single-channel | minutes (NumPy) | inference |

All numbers except DeepFilterNet2's paper RTF are unverified for this hardware; M4 benchmarking should measure them with the actual fixtures.

## Recommended default chain (safe defaults)

```
[0a] ffprobe  -> duration, sample_rate, channels, codec                       (metadata for UI + policy)
[0b] channel policy
       channels==1            -> as is
       channels==2, correlated -> ffmpeg -ac 1 (average)                       (default)
       channels==2, independent (telephony heuristic) -> split L/R, per-channel pipeline, skip diarization
       channels>2             -> average (or user-picked index)
[0c] ffmpeg -nostdin -threads 0 -i IN [-map/-af pan] -f s16le -ac 1 -acodec pcm_s16le -ar 16000 -
       -> float32 in [-1,1], 8 kHz sources upsampled by the same call
[0d] peak-safety gain only: if peak < -20 dBFS, apply linear gain to -3 dBFS (no compression, no loudnorm)
[0e] clipping detection -> warning only (no de-clipper)
[1]  FSMN-VAD on the 16 kHz mono signal; produce segment list
[1b] long-file chunker: group VAD segments into ~5-min windows (ForcedAligner limit) for ASR/align,
     and ~20-30-min windows with ~30 s overlap for CAM++/pyannote; reconcile speakers across windows by
     centroid cosine similarity; cloud path: split at VAD silence to <= 2 h when diarization_enabled
```

Rationale: steps 0a-0c mirror WhisperX/FunASR exactly (findings 1-2) and are what every downstream model was trained on. 0d is cheap insurance for whisper-quiet recordings and is gain-only, so it cannot introduce artifacts (finding 6/9). Nothing non-linear is applied by default because three independent studies show enhancement hurts modern ASR (findings 7-9) and no evidence shows it helps CAM++/pyannote (finding 11). Chunking rules come from the ForcedAligner 5-min limit, the DashScope 2-h diarization cap and pyannote/NeMo windowing practice (findings 15-17).

## Optional user toggles (off by default)

| Toggle | When to suggest | Implementation note |
|---|---|---|
| `denoise: deepfilternet` | steady background noise (HVAC, fan) with audible impact; user listens first | 16k->48k->DFN3->16k; blend with original (OA, a~0.2); run on a copy, keep raw for diarization unless user opts in |
| `denoise: clearvoice_frcrn_16k` | same, Chinese-centric stack; avoids the 48 k round trip | ModelScope model, same lab as FunASR; needs CPU RTF measurement |
| `vocal_separation: demucs` | recordings with music beds (podcasts, event videos) | whisper-diarization precedent; chunk >1 h to bound RAM; expect 10-30 min/h on CPU |
| `dereverb: wpe` | far-field conference-room mic, strong echo | linear, low artifact risk; run before downmix if multi-channel |
| `loudness: ebu_r128` (2-pass `loudnorm`) | very uneven speaker levels | 2-pass only; raises noise floor during pauses, so pair with VAD |
| `channels: left/right/per-channel/downmix` | user knows the recording layout | overrides the auto heuristic; per-channel maps to DashScope `channel_id=[0,1]` |
| `telephony_8k_mode` | 8 kHz input | currently just upsampling; placeholder for future 8 k CAM++ model |
| `resemble_enhance` | **not recommended** on CPU | generative; alters timbre; hours per hour of audio |

Each toggle should be benchmarked in M4 with CER / term recall / DER against the raw-audio baseline before being surfaced in the Settings page; the default UI copy should state that denoising usually does *not* improve accuracy.

## Contradictions
- LLM search summaries claimed FunASR's loader has a `channel_id` argument; the fetched `load_utils.py` has none (only `reduce_channels` averaging / channel-0 fallback). Trust the source.
- Marketing-style pages (forasoft, noisereducerai, dev.to "clean audio before Whisper") assert denoising improves Whisper accuracy; the three peer-reviewed/arXiv studies (findings 7-9) report the opposite across every configuration tested. Deprioritized the blogs.
- whisper-diarization enables demucs by default, while WhisperX/FunASR/pyannote/NeMo do nothing; the divergence is explained by domain (music-laden media vs. speech-only), per its own `--no-stem` help text.

## Missing evidence
- `source_check` returned no passages for the central "denoising hurts ASR" claim; verification was done by fetching the arXiv abstracts directly (2512.17562, 2603.04710, 2201.06685).
- No study tests enhancement in front of Qwen3-ASR, CAM++, or FSMN-VAD specifically; generalization is researcher inference.
- No published CPU RTF for ClearerVoice FRCRN/MossFormer2, demucs on Ryzen, nara_wpe; the table values are estimates.
- No measured effect of `loudnorm` / peak gain / 8 k-upsampling on CER or DER.
- The WPE "10-25% DER reduction" figure appeared only in an LLM summary and could not be traced to a source.

## Sources
- Kept: whisperx/audio.py (https://raw.githubusercontent.com/m-bain/whisperX/main/whisperx/audio.py) — canonical ffmpeg load, no extra processing
- Kept: FunASR load_utils.py (https://raw.githubusercontent.com/modelscope/FunASR/main/funasr/utils/load_utils.py) — same ffmpeg path, channel averaging, no channel_id
- Kept: whisper-diarization diarize.py (https://raw.githubusercontent.com/MahmoudAshraf97/whisper-diarization/main/diarize.py) — demucs-by-default precedent and its caveat
- Kept: Chondhekar et al. 2025, arXiv:2512.17562 — enhancement hurts 4 modern ASR systems in 40/40 configs
- Kept: Islam et al. 2026, arXiv:2603.04710 — separation raises PSNR but WER/CER in every config
- Kept: Iwamoto et al. 2022, arXiv:2201.06685 — artifacts are the cause; observation-adding mitigation
- Kept: Schröter et al. 2022, arXiv:2205.05474 — DeepFilterNet2 RTF 0.04 on notebook CPU
- Kept: modelscope/ClearerVoice-Studio README — FRCRN/MossFormer model inventory, CPU install, NumPy API
- Kept: Alibaba Cloud non-real-time ASR guide — channel_id, any sample rate, 12 h/2 GB, 2 h cap with diarization, ffmpeg FAQ
- Kept: QwenLM/Qwen3-ASR README — input types, 16 kHz streaming demo, ForcedAligner 5-min limit
- Kept: pyannote-audio#1966 — long-audio chunk + reconcile guidance (community, medium weight)
- Kept: NeMo diarization datasets page — windowed manifests on long recordings
- Rejected/deprioritized: forasoft / noisereducerai / dev.to / gigagpu blogs — SEO content asserting denoising helps, no data
- Rejected/deprioritized: tomodahinata / stemsplit demucs CPU numbers — unsourced third-party benchmarks (used only as low-confidence estimate)

## Next steps
- M4: benchmark raw vs. DeepFilterNet3 (with and without OA blend) vs. FRCRN_SE_16K on the 3-5 min fixtures with Qwen3-ASR-0.6B + CAM++; record CER, term recall, DER, wall-clock.
- Implement the telephony channel-independence heuristic and validate on one real 2-channel call recording.
- Decide the long-file chunk/overlap sizes empirically once CAM++ memory profile on CPU is known.
