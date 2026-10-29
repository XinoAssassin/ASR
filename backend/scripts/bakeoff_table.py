"""Merge bake-off reports (local + cloud) into one Markdown table, sorted by lenient CER.

    uv run --no-sync python scripts/bakeoff_table.py data/bakeoff/part1 data/bakeoff/part2 ...

Reads report.json (local, scripts/bakeoff.py) and cloud_*_report.json (cloud_flash_eval.py,
cloud_realtime_eval.py, the filetrans baseline) from each directory. All directories must come
from the same excerpt (identical chunks.npz), which is checked.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def main() -> None:
    dirs = [Path(d) for d in sys.argv[1:]]
    digests = {hashlib.sha256((d / "chunks.npz").read_bytes()).hexdigest() for d in dirs}
    if len(digests) != 1:
        raise SystemExit("reports come from different excerpts (chunks.npz differ)")
    rows = []
    for d in dirs:
        local = d / "report.json"
        if local.exists():
            for r in json.loads(local.read_text(encoding="utf-8"))["rows"]:
                if "error" not in r:
                    rows.append({**r, "where": "local"})
        for cloud in sorted(d.glob("cloud_*_report.json")):
            for r in json.loads(cloud.read_text(encoding="utf-8"))["rows"]:
                if "error" not in r:
                    rows.append({**r, "where": "cloud", "dtype": "-"})
    rows.sort(key=lambda r: (r["cer_lenient"], r["cer"]))
    print(
        "| # | Model | Where | dtype | Terms in | CER raw | CER lenient | S / D / I "
        "| Term hits | RTF | Peak RSS |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(rows, 1):
        rtf = r.get("rtf", r.get("wall_rtf"))
        rtf_s = (
            "-" if rtf is None else (f"{rtf:.3f}" if r["where"] == "local" else f"{rtf:.2f} (wall)")
        )
        rss = f"{r['peak_rss_mb'] / 1024:.1f} GB" if r.get("peak_rss_mb") else "-"
        print(
            f"| {i} | `{r['model']}` | {r['where']} | {r.get('dtype', '-')} | "
            f"{'yes' if r.get('terms') else 'no'} | {r['cer']:.2%} | **{r['cer_lenient']:.2%}** | "
            f"{r['sub']} / {r['del']} / {r['ins']} | {r['term_hits']} | {rtf_s} | {rss} |"
        )


if __name__ == "__main__":
    main()
