"""M11: English-only route — faster-whisper vs Qwen3-ASR through the full local pipeline.

    uv run --no-sync python scripts/english_eval.py --clips /mnt/d/tmp/asr-m11/libri/clips \
        --models faster-whisper-small,qwen3-asr-0.6b --cache models

``--clips`` is a directory with ``meta.json`` (``[{"file", "text"}]``, e.g. LibriSpeech) and the
audio files. Every clip runs through ``LocalPipelineProvider`` (FSMN-VAD -> ASR -> alignment or
the engine's own word times -> merge) with ``language_hints=["en"]`` and one speaker, so the
numbers include VAD and alignment. Reports WER (``mixed_error_rate`` on Latin words), RTF
(processing time / audio time, model load excluded) and the share of segments with word times.

``--dialogue FILE --dialogue-ref FILE`` additionally runs one multi-speaker file with
diarization and prints the segments, to check that the word times feed CAM++ / merge.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from asr.audio.decode import decode_pcm16k
from asr.eval.metrics import mixed_error_rate
from asr.providers.base import AudioSource, JobOptions, LocalOptions, ProgressEvent
from asr.providers.local.pipeline import LocalPipelineProvider, load_engines


def _noop(_: ProgressEvent) -> None:
    return None


def run_model(key: str, cache: Path, clips: Path, dtype: str) -> dict[str, object]:
    meta = json.loads((clips / "meta.json").read_text(encoding="utf-8"))
    t0 = time.perf_counter()
    engines = load_engines(cache, key, device="cpu", dtype=dtype)
    load_s = time.perf_counter() - t0
    provider = LocalPipelineProvider(cache_dir=cache, asr_model=key, engines=engines, dtype=dtype)
    options = JobOptions(
        provider="local", language_hints=["en"], num_speakers=1, local=LocalOptions(asr_model=key)
    )
    refs, hyps = [], []
    audio_s = proc_s = 0.0
    segs = timed = 0
    for i, item in enumerate(meta):
        path = clips / item["file"]
        audio_s += len(decode_pcm16k(path)) / 16_000
        if i == 0:  # warm-up (first-call kernel setup)
            provider.transcribe(AudioSource(path=path), options, _noop)
        t = time.perf_counter()
        transcript = provider.transcribe(AudioSource(path=path), options, _noop)
        proc_s += time.perf_counter() - t
        refs.append(item["text"])
        hyps.append(" ".join(s.text for s in transcript.segments))
        segs += len(transcript.segments)
        timed += sum(1 for s in transcript.segments if s.words)
    err = mixed_error_rate(" ".join(refs), " ".join(hyps))
    return {
        "model": key,
        "clips": len(meta),
        "audio_s": round(audio_s, 1),
        "wer": err.rate,
        "errors": err.errors,
        "ref_words": err.ref_len,
        "rtf": proc_s / audio_s,
        "load_s": round(load_s, 1),
        "segments_with_words": f"{timed}/{segs}",
        "hyps": hyps,
    }


def run_dialogue(key: str, cache: Path, path: Path, ref: Path | None, dtype: str) -> None:
    provider = LocalPipelineProvider(cache_dir=cache, asr_model=key, dtype=dtype)
    options = JobOptions(provider="local", language_hints=["en"], local=LocalOptions(asr_model=key))
    t = time.perf_counter()
    transcript = provider.transcribe(AudioSource(path=path), options, _noop)
    secs = time.perf_counter() - t
    print(f"\n### {key} on {path.name}: {secs:.1f} s, {len(transcript.speakers)} speakers")
    for s in transcript.segments:
        print(f"[{s.start:6.2f}-{s.end:6.2f}] {s.speaker_id} ({len(s.words)} words) {s.text}")
    if ref is not None:
        reference = ref.read_text(encoding="utf-8")
        hyp = " ".join(s.text for s in transcript.segments)
        print(f"WER {mixed_error_rate(reference, hyp).rate:.2%}")
    print("warnings:", transcript.warnings)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--clips", type=Path)
    p.add_argument("--models", default="faster-whisper-small,qwen3-asr-0.6b")
    p.add_argument("--cache", type=Path, default=Path("models"))
    p.add_argument("--dtype", default="auto")
    p.add_argument("--out", type=Path, help="write the per-model results (with hypotheses)")
    p.add_argument("--dialogue", type=Path)
    p.add_argument("--dialogue-ref", type=Path)
    args = p.parse_args()

    results = []
    for key in args.models.split(","):
        if args.clips:
            r = run_model(key, args.cache, args.clips, args.dtype)
            results.append(r)
            print(
                f"| {key} | {r['clips']} clips / {r['audio_s']} s | {r['wer']:.2%} "
                f"({r['errors']}/{r['ref_words']}) | {r['rtf']:.3f} | {r['load_s']} s | "
                f"{r['segments_with_words']} |",
                flush=True,
            )
        if args.dialogue:
            run_dialogue(key, args.cache, args.dialogue, args.dialogue_ref, args.dtype)
    if args.out:
        args.out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
