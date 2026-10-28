"""Cut the short real-recording test fixture (EXECUTION_LOG D10).

Reads ``<repo>/ASR_4people-test.m4a`` (a private 4-speaker meeting, never committed) and writes a
20-second excerpt starting at 60 s to ``<repo>/tests/fixtures/4people_20s.m4a`` (git-ignored).
The excerpt is re-encoded as AAC-LC with the source's sample rate and channel layout, so the
channel heuristic sees the same stereo structure. Uses PyAV only (no host ffmpeg).

    uv run python scripts/make_fixtures.py [--source PATH] [--start 60] [--duration 20] [--force]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import av
import numpy as np
from av.audio.resampler import AudioResampler

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = REPO_ROOT / "ASR_4people-test.m4a"
DEFAULT_OUTPUT = REPO_ROOT / "tests" / "fixtures" / "4people_20s.m4a"


def cut_excerpt(src: Path, dst: Path, *, start: float, duration: float) -> Path:
    """Decode ``[start, start + duration)`` of the first audio stream and encode it as AAC."""
    tmp = dst.with_name(f".{dst.name}.part")
    with av.open(str(src)) as inp:
        stream = inp.streams.audio[0]
        rate = int(stream.rate)
        layout = stream.layout.name
        channels = stream.layout.nb_channels
        first = round(start * rate)
        last = first + round(duration * rate)
        # Seek a little early, then trim sample-exactly using the decoded timestamps.
        inp.seek(max(0, int((start - 1.0) / stream.time_base)), stream=stream, backward=True)
        resampler = AudioResampler(format="fltp", layout=layout, rate=rate)
        collected: list[np.ndarray] = []
        position: int | None = None
        for frame in inp.decode(stream):
            for out in resampler.resample(frame):
                block = out.to_ndarray()
                if position is None:
                    pts = out.pts if out.pts is not None else 0
                    position = round(float(pts * out.time_base) * rate) if out.time_base else 0
                lo, hi = position, position + block.shape[1]
                position = hi
                if hi <= first:
                    continue
                collected.append(block[:, max(0, first - lo) : min(block.shape[1], last - lo)])
            if position is not None and position >= last:
                break
    if not collected:
        raise SystemExit(f"no audio decoded from {src} at {start}s")
    samples = np.ascontiguousarray(np.concatenate(collected, axis=1), dtype=np.float32)

    dst.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(tmp), mode="w", format="ipod") as out_container:
        enc = out_container.add_stream("aac", rate=rate, layout=layout)
        enc.bit_rate = 64_000 * channels
        for i in range(0, samples.shape[1], 4096):
            block = np.ascontiguousarray(samples[:, i : i + 4096])
            frame = av.AudioFrame.from_ndarray(block, format="fltp", layout=layout)
            frame.rate = rate
            frame.pts = i
            for packet in enc.encode(frame):
                out_container.mux(packet)
        for packet in enc.encode(None):
            out_container.mux(packet)
    tmp.replace(dst)
    return dst


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--start", type=float, default=60.0)
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--force", action="store_true", help="overwrite an existing excerpt")
    args = parser.parse_args(argv)

    if not args.source.is_file():
        print(f"source recording not found: {args.source}", file=sys.stderr)
        return 1
    if args.output.exists() and not args.force:
        print(f"exists, keeping: {args.output} (use --force to rebuild)")
        return 0
    path = cut_excerpt(args.source, args.output, start=args.start, duration=args.duration)
    print(f"wrote {path} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
