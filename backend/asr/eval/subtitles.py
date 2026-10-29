"""Parse subtitle files (ASS / SRT / VTT) into timed cues used as reference transcripts."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Cue:
    start: float  # seconds
    end: float
    text: str


_ASS_TAG = re.compile(r"\{[^}]*\}")
_HTML_TAG = re.compile(r"<[^>]+>")
_SRT_TIME = re.compile(
    r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{1,3})"
)
_VTT_TIME = re.compile(
    r"(?:(\d+):)?(\d{2}):(\d{2})\.(\d{1,3})\s*-->\s*(?:(\d+):)?(\d{2}):(\d{2})\.(\d{1,3})"
)


def _secs(h: str | None, m: str, s: str, frac: str) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(frac.ljust(3, "0")[:3]) / 1000


def _ass_time(value: str) -> float:
    h, m, s = value.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def parse_ass(text: str) -> list[Cue]:
    cues: list[Cue] = []
    fields: list[str] | None = None
    in_events = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_events = line.lower() == "[events]"
            continue
        if not in_events:
            continue
        if line.lower().startswith("format:"):
            fields = [f.strip().lower() for f in line.split(":", 1)[1].split(",")]
        elif line.lower().startswith("dialogue:") and fields:
            values = line.split(":", 1)[1].split(",", len(fields) - 1)
            row = dict(zip(fields, (v.strip() for v in values), strict=False))
            body = _ASS_TAG.sub("", row.get("text", ""))
            body = body.replace("\\N", " ").replace("\\n", " ").replace("\\h", " ").strip()
            if body:
                cues.append(Cue(_ass_time(row["start"]), _ass_time(row["end"]), body))
    return sorted(cues, key=lambda c: c.start)


def _parse_blocks(text: str, pattern: re.Pattern[str], vtt: bool) -> list[Cue]:
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = block.strip().splitlines()
        for i, line in enumerate(lines):
            m = pattern.search(line)
            if not m:
                continue
            g = m.groups()
            start = _secs(g[0], g[1], g[2], g[3]) if vtt else _secs(*g[:4])
            end = _secs(g[4], g[5], g[6], g[7]) if vtt else _secs(*g[4:])
            body = " ".join(_HTML_TAG.sub("", t).strip() for t in lines[i + 1 :]).strip()
            if body:
                cues.append(Cue(start, end, body))
            break
    return sorted(cues, key=lambda c: c.start)


def parse_srt(text: str) -> list[Cue]:
    return _parse_blocks(text, _SRT_TIME, vtt=False)


def parse_vtt(text: str) -> list[Cue]:
    return _parse_blocks(text, _VTT_TIME, vtt=True)


def load_subtitles(path: Path) -> list[Cue]:
    text = Path(path).read_text(encoding="utf-8-sig")
    suffix = Path(path).suffix.lower()
    if suffix in (".ass", ".ssa"):
        return parse_ass(text)
    if suffix == ".srt":
        return parse_srt(text)
    if suffix == ".vtt":
        return parse_vtt(text)
    raise ValueError(f"unsupported subtitle format: {suffix}")


def shift(cues: Sequence[Cue], offset: float) -> list[Cue]:
    """Move cues by ``offset`` seconds (subtitle time + offset = audio time)."""
    return [Cue(c.start + offset, c.end + offset, c.text) for c in cues]


def select_window(
    cues: Sequence[Cue], start: float, end: float, *, pad: float = 0.4
) -> tuple[list[Cue], float, float]:
    """Cues lying fully inside ``[start, end]`` and the audio span to cut for them.

    The span is padded by up to ``pad`` seconds, but never into a neighbouring cue, so the audio
    excerpt contains exactly the speech the reference covers.
    """
    inside = [c for c in cues if c.start >= start and c.end <= end]
    if not inside:
        raise ValueError("no subtitle cue lies fully inside the window")
    before = [c.end for c in cues if c.end <= inside[0].start]
    after = [c.start for c in cues if c.start >= inside[-1].end]
    lo = inside[0].start - pad
    hi = inside[-1].end + pad
    if before:
        lo = max(lo, (before[-1] + inside[0].start) / 2)
    if after:
        hi = min(hi, (after[0] + inside[-1].end) / 2)
    return inside, max(lo, 0.0), hi


def estimate_offset(
    speech: Sequence[tuple[float, float]],
    cues: Sequence[Cue],
    *,
    search: float = 10.0,
    step: float = 0.05,
) -> tuple[float, float]:
    """Offset (seconds) that best aligns subtitle cues with detected speech, and its overlap score.

    Maximises the overlap between the VAD speech mask and the shifted cue mask; the score is the
    fraction of cue time covered by speech at the best offset.
    """
    if not speech or not cues:
        return 0.0, 0.0
    res = 0.05
    horizon = max(max(e for _, e in speech), max(c.end for c in cues)) + search + 1
    n = int(horizon / res) + 1
    speech_mask = bytearray(n)
    for s, e in speech:
        for i in range(int(s / res), min(int(e / res) + 1, n)):
            speech_mask[i] = 1
    cue_bins = [(int(c.start / res), int(c.end / res)) for c in cues]
    cue_total = sum(b - a for a, b in cue_bins) or 1
    best = (0.0, -1.0)
    steps = int(search / step)
    for k in range(-steps, steps + 1):
        shift_bins = round(k * step / res)
        hit = 0
        for a, b in cue_bins:
            lo, hi = max(a + shift_bins, 0), min(b + shift_bins, n)
            if hi > lo:
                hit += sum(speech_mask[lo:hi])
        score = hit / cue_total
        if score > best[1]:
            best = (k * step, score)
    return best
