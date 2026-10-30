# Research: AMD XDNA 2 NPU (Strix Halo) — what it supports and whether it helps this pipeline

Date: 2026-10-30. Context: PLAN §4.3 local pipeline (FSMN-VAD → Qwen3-ASR 0.6B/1.7B via transformers →
Qwen3-ForcedAligner → CAM++ + clustering → merge; optional L3 LLM / summaries via DashScope `qwen-plus`).
Backend runs in WSL2, Docker or on a NAS. Companion work: a Linux ROCm (iGPU) backend is being built separately.

Evidence tags used below: **[verified]** = observed on this machine on 2026-10-30 (read-only commands);
**[docs]** = vendor documentation / release notes; **[community]** = third-party reports or issues;
**[inference]** = my reasoning, not stated by a source.

## Bottom line

- The NPU is healthy and fully provisioned on **Windows** (driver, XRT 2.21, firmware 1.1.2.65, and even the
  Windows ML VitisAI EP arrived via Windows Update) — but it is **invisible to WSL2 and to Docker Desktop**, where our
  backend runs. The WSL kernel is built without `CONFIG_DRM_ACCEL`, and WSL only forwards the GPU via `/dev/dxg`.
- **No Chinese-capable ASR model runs on the AMD NPU today.** The only speech models with NPU support are
  OpenAI Whisper (base/small/medium/large-v3-turbo) and an English Zipformer. Whisper's Chinese meeting CER is ~3×
  ours (`docs/ASR_MODEL_COMPARISON.md`), and even on the NPU it is barely faster than CPU whisper.cpp.
- VAD, CAM++ and the forced aligner have **no NPU ports**; they are small and already cheap on CPU.
- The one realistic use is **local LLMs** (Qwen3 / Qwen3.5 up to 9B, Qwen3.6-35B-A3B) through FastFlowLM or Lemonade on the
  Windows host, exposed as an OpenAI-compatible HTTP server that the WSL backend could call. But the iGPU (llama.cpp
  Vulkan/ROCm) runs larger models faster on this machine.
- **Recommendation: don't build an NPU path for ASR.** Keep CPU bf16 plus the ROCm iGPU path. If we want a local LLM, make the
  L3/summary endpoint a pluggable OpenAI-compatible base URL (about 1–2 days). Users can then point it at Lemonade/FastFlowLM
  (NPU) or llama.cpp (iGPU) on the host. Details in §7.

## 1. Machine facts [verified]

| Item | Value | How |
|---|---|---|
| CPU | AMD RYZEN AI MAX+ 392 w/ Radeon 8060S (12C/24T Zen 5, Strix Halo) | `Win32_Processor`, `xrt-smi examine` |
| System | Lenovo model 83V9, BIOS UACN26WW, 64 GB (WSL capped at 32 GB via `.wslconfig`) | `xrt-smi examine`, `.wslconfig` |
| Windows | Windows 11 Pro 26H2, build 26300.9711 | registry / `Win32_OperatingSystem` |
| NPU device | "NPU Compute Accelerator Device", PCI `VEN_1022&DEV_17F0` rev 0x11, status OK, class ComputeAccelerator | `Get-PnpDevice` |
| NPU driver | AMD 32.0.20102.3930, dated 2026-07-05 (`oem136.inf`) | `Win32_PnPSignedDriver` |
| XRT / firmware | XRT 2.21.0 (hash date 2026-05-07), NPU firmware 1.1.2.65, device name "NPU Strix Halo", 8 columns, power mode Default | `C:\Windows\System32\AMD\xrt-smi.exe examine` |
| NPU overlays | `AMD_AIE2P_4x4_Overlay_*` / `Nx4_Overlay` xclbins + `pyxrt.pyd` ship with the driver in `C:\Windows\System32\AMD` | directory listing |
| iGPU driver | Radeon 8060S, 32.0.22076.3001 (2026-07-15); AMD Software (Adrenalin) 26.6.1 | `Win32_PnPSignedDriver`, uninstall registry |
| Windows ML EPs | `WindowsWorkload.EP.AMD.VitisAI.1.8` 1.8.46.0 and `...VitisAI.Framework.1.8` 1.8.80.0 installed (Windows Update), WinMLShared 4.2/5.0/5.2 | `Get-AppxPackage` |
| Ryzen AI SW / Lemonade / FastFlowLM | **Not installed** (no `C:\Program Files\RyzenAI`, no `flm`, `lemonade-server`, conda) | Program Files + `Get-Command` |
| Host Python | 3.13 with `onnxruntime` 1.31.0 (providers: Azure, CPU only — no VitisAI) | `get_available_providers()` |
| WSL | WSL 2.7.8, kernel 6.18.33.1-microsoft-standard-WSL2, Arch Linux, `networkingMode=Mirrored` | `wsl --version`, `uname` |
| WSL devices | `/dev/dxg` only; no `/dev/accel`, `/dev/kfd`, `/dev/dri`; `lspci` shows only the virtual "Microsoft 3D controller" | `ls /dev`, `lspci` |
| WSL kernel config | `# CONFIG_DRM_ACCEL is not set`, `# CONFIG_HSA_AMD is not set` → amdxdna can't exist in this kernel | `/proc/config.gz` |

So the NPU works on the Windows host, and nothing in WSL can reach it. With mirrored networking, a server on the Windows host
(e.g. FastFlowLM on `127.0.0.1:52625`) is reachable from WSL at `127.0.0.1` **[inference from config]**.

## 2. The NPU itself [docs / community]

- **Generation:** XDNA 2 (AIE2P, "npu5" on Linux, PCI 1022:17f0 rev 0x11). It is the same NPU class as Strix Point / Krackan.
  Ryzen AI SW treats Strix Halo as "STX" ([relnotes](https://ryzenai.docs.amd.com/en/latest/relnotes.html)).
- **Peak:** 50 TOPS INT8, and AMD also claims 50 TOPS Block-FP16. The 392 is listed at 50 NPU TOPS / 122 platform TOPS
  ([VideoCardz, CES 2026](https://videocardz.com/newz/amd-launches-ryzen-ai-max-392-and-max-388-strix-halo-apus-with-radeon-8060s-graphics);
  [Tom's Hardware XDNA 2](https://www.tomshardware.com/pc-components/cpus/amd-deep-dives-zen-5-ryzen-9000-and-strix-point-cpu-rdna-35-gpu-and-xdna-2-architectures/5)).
- **Native data types:** int8, int16, bf16, and BFP16 (block FP16: 8 values share an 8-bit exponent, 7-bit mantissas). Software also
  exposes A8W8 / A16W8 / XINT8 (int compiler, Ryzen AI 1.6+) and 4-bit group-quantised weights for LLMs (OGA / FastFlowLM
  "Q4NX"/Q4_1/Q4_K). Measured full-array GEMM is about 38 TOPS int8 and about 15 TFLOPS plain bf16, so plain bf16 runs at
  roughly half the int8 rate ([emergentmind summary of Taka et al.](https://www.emergentmind.com/topics/amd-xdna-2-npu)) **[community]**.
- **Memory:** it shares LPDDR5X with CPU/iGPU, about 256 GB/s, which caps LLM decode on every engine.

## 3. Software stacks and OS support (state 2026-10)

| Stack | What it is | Windows | Linux (native) | WSL2 | Docker |
|---|---|---|---|---|---|
| **Ryzen AI Software 1.8.0** (2026-10-07) | ONNX Runtime + VitisAI EP, Quark quantiser, OGA (ONNX Runtime GenAI) LLM flows, whisper.cpp fork | Full: CNN INT8/BF16, NLP BF16, OGA hybrid (NPU+iGPU) and NPU-only LLMs | Ubuntu 24.04 + Python 3.12; "NPU only flow"; no iGPU EP; **model compile not supported on Linux** (models must be generated on Windows); docs name Strix Point and newer, Strix Halo not named | No | Not documented |
| **Windows ML** VitisAI EP | Same EP delivered/updated by Windows Update via `ExecutionProviderCatalog` | Yes (installed here) | – | – | – |
| **FastFlowLM (FLM) 1.0** (2026-08-11, now `ROCm/FastFlowLM`) | Closed NPU kernels + MIT runtime; LLM/VLM/Whisper/embeddings; OpenAI-compatible server on :52625 | Yes (`flm-setup.msi`); needs NPU driver ≥ 32.0.203.311 | Yes since 2026-03-11; needs kernel ≥ 7.0 (in-tree amdxdna) or amdxdna-dkms, NPU fw ≥ 1.1.0.0 | No | Yes on a Linux host (`--device /dev/accel/accel0`, `--ulimit memlock=-1`) |
| **Lemonade Server** (2026.44) | AMD's local server; recipes for OGA (NPU/hybrid), FLM (NPU), llama.cpp (Vulkan/ROCm), whisper.cpp | Yes | Yes (FLM, llama.cpp) | Client only | – |
| **whisper.cpp `-DWHISPER_VITISAI=1`** | Whisper encoder offloaded to NPU from precompiled `.rai` caches | Yes | README says Ubuntu 24.04 + Python 3.12; the AMD doc page still says "Windows only, Linux planned" | No | – |
| **amdxdna kernel driver + XRT** | Upstream since Linux 6.14; Strix Halo fw 1.1.2.65 needs the protocol-7 driver (kernel ≥ 7.0) | (Windows driver instead) | Yes, but version-sensitive (see below) | **No** — WSL exposes no PCI device; kernel lacks DRM_ACCEL | Linux host only |

Linux maturity caveats **[community]**:
- Firmware/driver protocol mismatches are common on 6.x kernels ([xdna-driver #1219](https://github.com/amd/xdna-driver/issues/1219),
  [#1469](https://github.com/amd/xdna-driver/issues/1469), [#1074](https://github.com/amd/xdna-driver/issues/1074)).
- A ZimaOS (NAS OS) test on a Max+ 395 published 2026-10-29 got XRT and IRON kernels running, but the VitisAI EP and FLM failed on
  kernel 6.18. Its conclusion: use the iGPU for production ([ZimaOS](https://www.zimaspace.com/docs/hardware/amd-ryzen-ai-npu-on-zimaos)).
- **Open bug, Strix Halo: NPU (FLM) together with ROCm on the iGPU causes NPU command timeouts and segfaults, and in one report a hard
  machine-check hang** ([xdna-driver #1605](https://github.com/amd/xdna-driver/issues/1605), Aug–Oct 2026, unresolved).
  This matters directly for us: it would mean the ROCm ASR backend running at the same time as an NPU LLM.
- The ONNX VitisAI EP on Linux shipped without `onnxruntime_providers_ryzenai.so` in 1.7 (RyzenAI-SW #333). 1.8 adds a Linux installer,
  but only for the NPU-only flow.

**WSL2 verdict:** WSL can't use the NPU. Neither Microsoft nor AMD documents NPU paravirtualisation (only GPU via `/dev/dxg`), no
xdna-driver issue reports it working, and this machine's WSL kernel can't load amdxdna **[verified + docs absence]**. To use the NPU,
code must run as a **native Windows process** (or on bare-metal Linux with kernel ≥ 7.0).

## 4. Capability matrix

Legend: ✅ works with published artefacts · 🟡 possible with own export/compile work, unproven · ❌ not available ·
(n) = note. "CPU" means our current PyTorch path in WSL/Docker. Status as of 2026-10.

| Task / model | NPU · Windows | NPU · Linux | NPU · WSL/Docker Desktop | iGPU 8060S | CPU (current) |
|---|---|---|---|---|---|
| **Qwen3-ASR 0.6B/1.7B** (our default) | ❌ (1) | ❌ | ❌ | ✅ PyTorch ROCm (Linux; Windows ROCm 7.2.1+/10.x wheels) | ✅ bf16 RTF 0.20 / 0.43 (M4) |
| Qwen3-ForcedAligner-0.6B | ❌ | ❌ | ❌ | ✅ PyTorch ROCm | ✅ |
| SenseVoice-Small / Paraformer (ONNX) | 🟡 (2) | 🟡 (compile on Windows) | ❌ | ✅ ROCm / MIGraphX / DirectML | ✅ Paraformer RTF 0.033 |
| Whisper base/small/medium/large-v3-turbo | ✅ Ryzen AI ONNX BF16, whisper.cpp VitisAI, FLM (turbo) (3) | ✅ FLM turbo; whisper.cpp VitisAI per README | ❌ | ✅ whisper.cpp Vulkan/ROCm | ✅ (not used: Chinese CER ~19 %) |
| FSMN-VAD / Silero VAD | 🟡 tiny, no port (4) | 🟡 | ❌ | n/a | ✅ negligible cost |
| CAM++ / ECAPA speaker embedding | 🟡 CNN INT8 flow plausible, no port (4) | 🟡 | ❌ | ✅ | ✅ |
| LLM Qwen3 0.6B–8B, Qwen3.5 0.8B–9B, Qwen3.6-35B-A3B | ✅ FLM, OGA NPU-only/hybrid (5) | ✅ FLM (kernel ≥ 7.0), OGA NPU-only | ❌ (call host server over HTTP ✅) | ✅ llama.cpp Vulkan/ROCm, larger and faster | ⚠️ slow |
| Embeddings (EmbeddingGemma-300m) | ✅ FLM / Ryzen AI 1.8 | ✅ FLM | ❌ | ✅ | ✅ |

Notes:
1. Qwen3-ASR is an audio encoder plus a Qwen3 LLM decoder. FLM ships Qwen3-0.6B/1.7B *text* kernels and Ryzen AI has a BF16
   encoder compiler, but nobody has published or shown Qwen3-ASR (or Fun-ASR-Nano, FireRedASR) on the NPU. FLM kernels are
   closed, so a new architecture depends on AMD/FLM. We could not do this ourselves. **[docs absence + inference]**
2. The VitisAI EP partitions ONNX graphs. Unsupported ops fall back to CPU. The BF16 compiler targets transformer encoders (1.6 notes
   "improves coverage and speed for ASR models"). SenseVoice's SAN-M encoder *might* compile. CIF (Paraformer) and dynamic shapes
   are likely CPU fallbacks. No sherpa-onnx / FunASR VitisAI port exists (sherpa-onnx targets RKNN/Ascend NPUs instead). **[inference]**
3. Numbers: Ryzen AI 1.8 whisper.cpp, 3 s clips, RTF 0.139 (small) / 0.337 (large-v3-turbo) **[docs]**. FLM whisper-v3-turbo on a
   Ryzen AI 9 365 under Linux, RTF ≈ 0.17–0.18 vs whisper.cpp CPU 16 threads ≈ 0.22: about the same speed, roughly 10× less energy
   ([npu-whisper](https://github.com/jac-76/npu-whisper), [dev.to](https://dev.to/jac-76/running-whisper-llms-on-an-amd-npu-under-linux-2o1h)) **[community]**.
   Lemonade CI measured whisper.cpp NPU ≈ CPU (2.1 s vs 1.9 s; ROCm 0.3 s) on short clips and proposed dropping it. The PR was
   closed unmerged as "non-reproducible" ([lemonade #3617](https://github.com/lemonade-sdk/lemonade/pull/3617)). The Ryzen AI ONNX demo
   puts 100 % of the encoder and 93 % of the decoder on the NPU for base. First compile takes about 15 min. With KV cache on, the decoder
   falls back to CPU ([AMD LIRA](https://github.com/amd/LIRA), archived 2026-09-30). FLM documents an OpenAI `/v1/audio/transcriptions`
   endpoint but no timestamp output.
4. These models are small (CAM++ ~7 M params, FSMN-VAD < 1 M). NPU dispatch/compile overhead would exceed any gain. **[inference]**
5. FLM throughput on Krackan Point (Ryzen AI 7 350, FLM 0.9.31). Strix Halo has the same NPU class:

   | Qwen3 (Q4_1) | decode tok/s @1k ctx | @8k ctx | prefill tok/s @4k |
   |---|---|---|---|
   | 0.6B | 66.5 | 31.0 | 2165 |
   | 1.7B | 40.2 | 23.7 | 1434 |
   | 4B | 19.6 | 13.7 | 615 |
   | 8B | 11.9 | 10.4 | 457 |

   Source: [FLM Qwen3 benchmarks](https://fastflowlm.com/docs/benchmarks/qwen3_results/). Tags: [FLM Qwen models](https://fastflowlm.com/docs/models/qwen/).
   For comparison on this iGPU **[community]**: llama.cpp Vulkan Qwen3-30B-A3B Q4 ≈ 86–100 tok/s decode, ~1.1–1.3 k tok/s prefill
   ([strix-halo-llm-perf](https://github.com/visorcraft/strix-halo-llm-perf), [strix-halo-guide](https://github.com/hogeheer499-commits/strix-halo-guide/blob/main/BENCHMARKS.md)).

## 5. iGPU vs NPU for our transformer models (brief)

- **The iGPU is the better target.** It has 40 RDNA 3.5 CUs, the same memory bandwidth, and full PyTorch support, so Qwen3-ASR,
  the aligner and CAM++ run unchanged with no export or quantisation step.
- ROCm Core SDK 10.1.0 (2026-10-05) lists gfx1151 (Max+ 392 included) on Ubuntu 24.04.5/26.04.1 and Windows 11 25H2, with PyTorch
  2.14.0 wheels for both ([ROCm release notes](https://rocm.docs.amd.com/en/latest/about/release-notes.html)) **[docs]**.
- ROCm 7.2.1 already shipped native-Windows PyTorch for Strix Halo
  ([ROCm blog](https://rocm.blogs.amd.com/artificial-intelligence/comfyui-windows/README.html)).
- **ROCm in WSL2:** 10.1 includes it as a "technical preview" in TheRock artefacts (Ubuntu .deb/wheels, no KFD tooling, device memory
  faults may hang the process). The official per-GPU WSL matrix still lists discrete Radeons only. A community recipe
  ([andweng/wsl-rocm](https://github.com/andweng/wsl-rocm), 2026-04) gets gfx1151 working via `/dev/dxg` with `hsa-runtime-rocr4wsl`
  and `librocdxg`. Our distro is Arch rather than Ubuntu, so expect friction **[docs + community]**.
- DirectML / WebGPU through ONNX Runtime are possible on Windows. They would need ONNX exports of autoregressive Qwen3-ASR, which is
  not worth it given native PyTorch ROCm.
- The NPU's advantages are power (around 10× better energy per token or transcribed second) and leaving the iGPU free. On a plugged-in
  desktop or NAS backend these matter less than accuracy and throughput, and the #1605 concurrency bug undercuts the "run both at once"
  idea on Linux today.

## 6. What using the NPU would actually take in this project

Every option needs a **native Windows process** (or bare-metal Linux ≥ 7.0), because the backend runs in WSL/Docker. Options, cheapest first:

| # | Option | Work | Effort | Value / risk |
|---|---|---|---|---|
| A | **Local LLM for L3 suggestions + summaries.** User runs FLM or Lemonade on the Windows host. Backend gets a configurable OpenAI-compatible `ASR_LLM_BASE_URL` (today `asr/correct/llm.py` hard-codes DashScope `…/compatible-mode/v1/chat/completions` behind an endpoint allow-list and requires a DashScope key) | Backend: base-URL setting, allow-list entry for loopback/configured host, optional key, JSON-mode fallback if the local server ignores `response_format`. Tests, settings UI field, docs | 1–2 days + 1 day quality check on our M4 sample | Useful on any host: the same setting covers llama.cpp/Ollama on the iGPU or a NAS. Risk: 4–9B local Qwen is weaker than `qwen-plus` at Chinese correction and summaries, and the NPU throughput (8–20 tok/s for 4–9B) makes long-meeting summaries take minutes |
| B | **Whisper on NPU** as an extra "local-npu" provider (Windows sidecar wrapping FLM `/v1/audio/transcriptions` or whisper.cpp VitisAI), plus our own alignment/diarization | Sidecar, provider, contract tests, Windows-only packaging | 4–6 days | **Not recommended.** Chinese meeting CER ~19 % vs our ~8.5–11 %, speed ≈ CPU, and no timestamps from FLM |
| C | **SenseVoice-Small / Paraformer ONNX on the VitisAI EP** (Windows sidecar or the Tauri desktop app). Export via FunASR/sherpa-onnx, BF16 compile (or Quark A16W8), measure op coverage | Export, compile per model (static shapes → fixed-length VAD chunks), CER re-check after BFP16/int8, sidecar IPC | 1–2 weeks, outcome uncertain | Low: these models are already fast on CPU (Paraformer RTF 0.033). They are less accurate than Qwen3-ASR and lack the term/context and alignment features |
| D | **Qwen3-ASR on NPU** (encoder via VitisAI BF16, decoder via an OGA/FLM-style Qwen3 kernel) | Needs AMD/FLM to publish kernels or OGA models for the architecture. Not doable from our side | Weeks+, vendor-blocked | Watch item only |
| E | VAD / CAM++ on NPU | ONNX export + INT8 CNN flow | 2–3 days | None: these stages are already a negligible share of runtime |

Cross-cutting requirements for B–E:
- Windows-only binaries (Ryzen AI 1.8 conda env or Windows ML EP), and Python 3.10–3.12 envs separate from our uv lock.
- Model compile caches (`.rai`, 15 min first compile).
- An IPC boundary that keeps `backend/asr/` web-free (AGENTS rule 5).
- Packaging inside `desktop/` if shipped to users.
- Accuracy for Chinese must be re-measured after BFP16/int8 quantisation. AMD reports "near-FP32" for Whisper only, with English test sets.

## 7. Recommendation

1. **No NPU work for the ASR pipeline now.** The NPU offers no Chinese ASR model, no aligner and no diarization model. Whisper, the
   one ASR it does run, is the model we rejected on accuracy, and it is not even faster than CPU here. Put acceleration effort into
   the **ROCm iGPU backend** (in progress) and keep CPU bf16 as the portable baseline. On a NAS, the NPU only matters if the NAS is
   itself a Ryzen AI box on kernel ≥ 7.0, and even then #1605 argues against running it next to ROCm.
2. **Optional, cheap, provider-agnostic: option A.** Make the LLM endpoint a configurable OpenAI-compatible base URL (1–2 days).
   That unlocks fully-offline summaries and suggestions via Lemonade/FastFlowLM (NPU) or llama.cpp (iGPU) on the Windows host,
   reachable from WSL at `127.0.0.1` thanks to mirrored networking. For quality, prefer the iGPU with Qwen3-30B-A3B /
   Qwen3.6-35B-A3B over NPU 4–9B models, unless the iGPU is busy with ASR. Validate on the M4 sample before advertising it.
3. **Re-evaluate** if any of these happens:
   - AMD/FLM publishes a Qwen3-ASR, SenseVoice or Paraformer NPU model.
   - Ryzen AI on Linux gains model compilation and Strix Halo is named.
   - xdna-driver #1605 is fixed.
   - WSL gains accelerator passthrough.
   A quick check at each Ryzen AI release (release notes + the HF `amd/ryzen-ai-whisper-npu-optimized-onnx-models` collection) is enough.

## 8. Not verified here

- No model was run on the NPU: nothing was installed. Throughput numbers come from docs and community reports, measured on Strix Point or
  Krackan Point rather than this 392.
- I did not check whether FLM's server honours `response_format: json_object`, which `chat_json` relies on.
- Whether driver 32.0.20102.3930 meets FLM's "≥ 32.0.203.311" is unconfirmed: the version schemes differ. The 2026-07 date and fw
  1.1.2.65 suggest it does.

## Sources

- Ryzen AI SW 1.8 release notes: https://ryzenai.docs.amd.com/en/latest/relnotes.html (1.8.0 PDF dated 2026-10-07)
- Ryzen AI Linux install: https://ryzenai.docs.amd.com/en/latest/linux.html
- Ryzen AI LLM overview: https://ryzenai.docs.amd.com/en/latest/llm/overview.html
- whisper.cpp VitisAI section: https://github.com/ggml-org/whisper.cpp · AMD fork https://github.com/amd/whisper.cpp
- RyzenAI-SW Whisper demo: https://github.com/amd/RyzenAI-SW/blob/main/Demos/ASR/Whisper/README.md · LIRA https://github.com/amd/LIRA
- FastFlowLM: https://github.com/ROCm/FastFlowLM · models https://fastflowlm.com/docs/models/ · Whisper https://fastflowlm.com/docs/models/whisper/ · benchmarks https://fastflowlm.com/docs/benchmarks/qwen3_results/
- FLM on Linux via Lemonade: https://lemonade-server.ai/flm_npu_linux.html · Lemonade releases https://github.com/lemonade-sdk/lemonade/releases · PR #3617 https://github.com/lemonade-sdk/lemonade/pull/3617 · issue #1968 https://github.com/lemonade-sdk/lemonade/issues/1968
- Linux driver: https://docs.kernel.org/accel/amdxdna/amdnpu.html · https://github.com/amd/xdna-driver (issues #1074, #1219, #1469, #1605)
- ZimaOS Strix Halo NPU test (2026-10-29): https://www.zimaspace.com/docs/hardware/amd-ryzen-ai-npu-on-zimaos
- Windows ML EPs: https://learn.microsoft.com/en-us/windows/ai/new-windows-ml/supported-execution-providers · AMD article https://www.amd.com/en/developer/resources/technical-articles/2026/ai-model-deployment-using-windows-ml-on-amd-npu.html
- XDNA 2 data types / TOPS: https://www.tomshardware.com/pc-components/cpus/amd-deep-dives-zen-5-ryzen-9000-and-strix-point-cpu-rdna-35-gpu-and-xdna-2-architectures/5 · https://www.emergentmind.com/topics/amd-xdna-2-npu
- Max+ 392 launch: https://videocardz.com/newz/amd-launches-ryzen-ai-max-392-and-max-388-strix-halo-apus-with-radeon-8060s-graphics
- ROCm 10.1.0 release notes (2026-10-05): https://rocm.docs.amd.com/en/latest/about/release-notes.html · WSL recipe https://github.com/andweng/wsl-rocm · Windows PyTorch blog https://rocm.blogs.amd.com/artificial-intelligence/comfyui-windows/README.html
- iGPU LLM numbers: https://github.com/visorcraft/strix-halo-llm-perf · https://github.com/hogeheer499-commits/strix-halo-guide/blob/main/BENCHMARKS.md
- Community NPU Whisper: https://github.com/jac-76/npu-whisper · https://dev.to/jac-76/running-whisper-llms-on-an-amd-npu-under-linux-2o1h
