"""M4 local ASR bake-off: CER / term recall / CPU RTF against a subtitle reference.

    uv run --extra cpu --extra local python scripts/bakeoff.py \\
        --audio ../ASR_4people-test.m4a --subs ../tests/fixtures/4people.ass \\
        --start 870 --end 1110 --terms-file ../tests/fixtures/4people_terms.txt

Pipeline per run (identical for every model): decode 16 kHz mono (PyAV) -> FSMN-VAD on the whole
file (also used to estimate the subtitle offset) -> reference = subtitle cues fully inside the
window -> VAD segments inside the excerpt grouped into <= 25 s chunks -> each model transcribes the
same chunks, with and without the term list. Each model runs in its own subprocess so peak RSS is
per model. Raw hypotheses and the JSON report go to --out (default data/bakeoff, git-ignored).
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from asr.audio.decode import decode_pcm16k
from asr.eval.metrics import mixed_error_rate, term_recall
from asr.eval.subtitles import estimate_offset, load_subtitles, select_window, shift

SR = 16_000
ASR_KEYS = ("seaco-paraformer", "fun-asr-nano", "qwen3-asr-0.6b", "qwen3-asr-1.7b")


def _cache_dir() -> Path:
    return Path(os.environ.get("ASR_MODEL_CACHE_DIR", "").strip() or "./models").resolve()


# ------------------------------------------------------------------------------------- worker
def run_worker(args: argparse.Namespace) -> None:
    """Load one model, transcribe the prepared chunks twice (no terms / terms), print JSON."""
    import torch

    torch.set_num_threads(args.threads)
    prep = np.load(args.chunks, allow_pickle=False)
    chunks = [prep[f"c{i}"] for i in range(int(prep["n"]))]
    terms = json.loads(Path(args.terms_json).read_text(encoding="utf-8"))
    cache = _cache_dir()

    t0 = time.perf_counter()
    if args.worker == "seaco-paraformer":
        from asr.providers.local.asr_paraformer import ParaformerAsr

        engine = ParaformerAsr(cache, dtype=args.dtype)
    else:
        from asr.providers.local.asr_transformers import TransformersAsr

        engine = TransformersAsr(cache, args.worker, dtype=args.dtype)
    load_s = time.perf_counter() - t0
    engine.transcribe(chunks[:1], terms=terms)  # warm-up, not timed

    runs = {}
    for label, run_terms in (("plain", []), ("terms", terms)):
        t0 = time.perf_counter()
        texts = engine.transcribe(chunks, terms=run_terms)
        runs[label] = {"seconds": time.perf_counter() - t0, "texts": texts}
    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(json.dumps({"load_s": load_s, "peak_rss_mb": peak_mb, "runs": runs}, ensure_ascii=False))


# ------------------------------------------------------------------------------------- driver
def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--audio", type=Path)
    p.add_argument("--subs", type=Path)
    p.add_argument("--start", type=float, help="window start, subtitle time (s)")
    p.add_argument("--end", type=float, help="window end, subtitle time (s)")
    p.add_argument("--terms-file", type=Path, help="one term per line")
    p.add_argument("--models", default=",".join(ASR_KEYS))
    p.add_argument("--threads", type=int, default=min(16, os.cpu_count() or 4))
    p.add_argument(
        "--dtypes",
        default="float32,bfloat16",
        help="comma-separated; bfloat16 uses AVX512_BF16 / AMX kernels where available",
    )
    p.add_argument("--offset", default="auto", help="subtitle offset in seconds, or 'auto'")
    p.add_argument("--max-chunk", type=float, default=25.0)
    p.add_argument("--out", type=Path, default=Path("data/bakeoff"))
    # internal
    p.add_argument("--worker", help=argparse.SUPPRESS)
    p.add_argument("--chunks", help=argparse.SUPPRESS)
    p.add_argument("--terms-json", help=argparse.SUPPRESS)
    p.add_argument("--dtype", default="auto", help=argparse.SUPPRESS)
    args = p.parse_args()
    if args.worker:
        run_worker(args)
        return

    from asr.providers.local.vad import FsmnVad, cut, merge_segments

    args.out.mkdir(parents=True, exist_ok=True)
    terms = []
    if args.terms_file:
        terms = [
            t.strip()
            for t in args.terms_file.read_text(encoding="utf-8").splitlines()
            if t.strip() and not t.startswith("#")
        ]

    t0 = time.perf_counter()
    audio = decode_pcm16k(args.audio)
    decode_s = time.perf_counter() - t0
    vad = FsmnVad(_cache_dir())
    t0 = time.perf_counter()
    speech = vad.segments(audio)
    vad_s = time.perf_counter() - t0
    cues = load_subtitles(args.subs)
    if args.offset == "auto":
        offset, overlap = estimate_offset(speech, cues)
    else:
        offset, overlap = float(args.offset), float("nan")
    cues = shift(cues, offset)
    ref_cues, a0, a1 = select_window(cues, args.start + offset, args.end + offset)
    reference = "".join(c.text for c in ref_cues)
    excerpt = audio[int(a0 * SR) : int(a1 * SR)]
    dur = len(excerpt) / SR
    local_speech = [(max(s, a0) - a0, min(e, a1) - a0) for s, e in speech if e > a0 and s < a1]
    spans = merge_segments(local_speech, max_len=args.max_chunk)
    chunks = cut(excerpt, spans)
    print(
        f"audio {len(audio) / SR:.1f}s decoded in {decode_s:.1f}s; VAD {vad_s:.1f}s; "
        f"subtitle offset {offset:+.2f}s (speech overlap {overlap:.2f}); "
        f"excerpt {a0:.2f}-{a1:.2f}s = {dur:.1f}s, {len(ref_cues)} cues, {len(chunks)} chunks",
        file=sys.stderr,
    )

    chunk_file = args.out / "chunks.npz"
    np.savez(chunk_file, n=len(chunks), **{f"c{i}": c for i, c in enumerate(chunks)})
    terms_file = args.out / "terms.json"
    terms_file.write_text(json.dumps(terms, ensure_ascii=False), encoding="utf-8")
    (args.out / "reference.txt").write_text(reference + "\n", encoding="utf-8")

    from asr.providers.local.device import cpu_features

    features = cpu_features()
    dtypes = [d.strip() for d in args.dtypes.split(",") if d.strip()]
    keys = [k.strip() for k in args.models.split(",") if k.strip()]
    rows = []
    for key, dtype in [(k, d) for k in keys for d in dtypes]:
        print(f"== {key} [{dtype}]", file=sys.stderr)
        cmd = [
            sys.executable,
            __file__,
            "--worker",
            key,
            "--chunks",
            str(chunk_file),
            "--terms-json",
            str(terms_file),
            "--threads",
            str(args.threads),
            "--dtype",
            dtype,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            print(proc.stderr[-3000:], file=sys.stderr)
            rows.append(
                {"model": key, "dtype": dtype, "error": proc.stderr.strip().splitlines()[-1:]}
            )
            continue
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        for label, run in result["runs"].items():
            hyp = "".join(run["texts"])
            (args.out / f"{key}.{dtype}.{label}.txt").write_text(
                "\n".join(run["texts"]) + "\n", encoding="utf-8"
            )
            err = mixed_error_rate(reference, hyp)
            lenient = mixed_error_rate(reference, hyp, drop_fillers=True)
            tr = term_recall(reference, hyp, terms)
            row = {
                "model": key,
                "dtype": dtype,
                "terms": label == "terms",
                "cer": round(err.rate, 4),
                "cer_lenient": round(lenient.rate, 4),
                "sub": err.substitutions,
                "del": err.deletions,
                "ins": err.insertions,
                "ref_tokens": err.ref_len,
                "term_recall": round(tr.recall, 3),
                "term_hits": f"{tr.found}/{tr.expected}",
                "per_term": tr.per_term,
                "seconds": round(run["seconds"], 1),
                "rtf": round(run["seconds"] / dur, 3),
                "load_s": round(result["load_s"], 1),
                "peak_rss_mb": round(result["peak_rss_mb"]),
            }
            rows.append(row)
            print(
                f"   {dtype:8} {label:5} CER {row['cer']:.2%} "
                f"(lenient {row['cer_lenient']:.2%})  terms {row['term_hits']}  "
                f"RTF {row['rtf']}  RSS {row['peak_rss_mb']} MB",
                file=sys.stderr,
            )

    report = {
        "audio": str(args.audio),
        "window_subtitle_time": [args.start, args.end],
        "subtitle_offset": offset,
        "speech_overlap": overlap,
        "excerpt": [a0, a1],
        "excerpt_seconds": dur,
        "chunks": len(chunks),
        "threads": args.threads,
        "cpu_capability": features.capability,
        "cpu_bf16": features.bf16,
        "terms": terms,
        "rows": rows,
    }
    (args.out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
