"""Score a hand-labelled speaker sheet (PLAN M5 acceptance: speaker mis-assignment < 10 %).

    uv run --no-sync python scripts/score_speaker_sheet.py SHEET.csv \
        --subs ../tests/fixtures/4people.ass

The sheet has one row per subtitle cue with columns ``no, video_time, text, local, cloud, ref``.
``ref`` is the human label; ``X`` or blank rows are skipped. Rows are weighted by cue duration
(looked up in the subtitles by ``video_time``); each system is scored under its best 1:1 label
mapping onto the reference letters, and lines a system left unassigned count as errors.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from asr.eval.diarization import speaker_confusion
from asr.eval.subtitles import load_subtitles


def _seconds(stamp: str) -> float:
    minutes, seconds = stamp.split(":")
    return int(minutes) * 60 + float(seconds)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("sheet", type=Path)
    p.add_argument("--subs", type=Path, required=True)
    args = p.parse_args()

    cues = load_subtitles(args.subs)
    by_start = {round(c.start, 2): c for c in cues}
    rows = list(csv.DictReader(args.sheet.open(encoding="utf-8-sig")))
    ref: list[tuple[float, float, str]] = []
    systems: dict[str, list[tuple[float, float, str]]] = {"local": [], "cloud": []}
    unassigned = {"local": 0.0, "cloud": 0.0}
    skipped = 0
    for row in rows:
        label = row["ref"].strip().upper()
        cue = by_start.get(round(_seconds(row["video_time"]), 2))
        if not label or label == "X" or cue is None:
            skipped += 1
            continue
        ref.append((cue.start, cue.end, label))
        for name, turns in systems.items():
            hyp = row[name].strip().upper()
            if hyp in ("", "-", "?"):  # the system gave this line no speaker: always an error
                unassigned[name] += cue.end - cue.start
            else:
                turns.append((cue.start, cue.end, hyp))
    total = sum(e - s for s, e, _ in ref)
    print(f"scored {len(ref)} lines ({total:.1f} s of speech), skipped {skipped}")
    for name, turns in systems.items():
        c = speaker_confusion(ref, turns)
        wrong = c.misassigned_seconds + unassigned[name]
        print(
            f"{name:6} speaker mis-assignment {wrong / total:.1%}  ({wrong:.1f} s; "
            f"{unassigned[name]:.1f} s unassigned)"
        )


if __name__ == "__main__":
    main()
