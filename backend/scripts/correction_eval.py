"""M6: term-recall gain from [5b] L1 correction, re-scored offline on saved bake-off outputs.

    uv run --no-sync python scripts/correction_eval.py --run data/bakeoff/part1 \
        --run data/bakeoff/part3 --aliases ../tests/fixtures/4people_aliases.txt

For each saved hypothesis (``<model>.<dtype>.<plain|terms>.txt`` and ``cloud-*.txt``) it applies
L1 correction with the run's term list at several thresholds, with and without an alias set, and
prints CER / lenient CER / term hits. Aliases (``--aliases``, asr-hotword lines) model the
"learning loop" where a user records mis-hearings after a first pass; when they were collected
from the same clip the result is optimistic and must be labelled so.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from asr.correct import TermCorrector
from asr.eval.metrics import mixed_error_rate, term_recall
from asr.providers.base import Term
from server.routes.term_sets import parse_terms


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--run", type=Path, action="append", required=True)
    p.add_argument("--aliases", type=Path, help="asr-hotword lines (Target | alias ...)")
    p.add_argument("--thresholds", default="0.85,0.8")
    p.add_argument("--only", default="", help="substring filter on hypothesis file names")
    args = p.parse_args()

    base = args.run[0]
    reference = (base / "reference.txt").read_text(encoding="utf-8").strip()
    names = json.loads((base / "terms.json").read_text(encoding="utf-8"))
    plain_terms = [Term(term=n) for n in names]
    alias_terms: list[Term] = []
    if args.aliases:
        alias_terms, errors = parse_terms(args.aliases.read_text(encoding="utf-8"))
        if errors:
            raise SystemExit(f"bad alias file: {errors}")
        known = {t.term.casefold() for t in alias_terms}
        alias_terms += [t for t in plain_terms if t.term.casefold() not in known]
    thresholds = [float(t) for t in args.thresholds.split(",")]

    variants: list[tuple[str, list[Term] | None, float]] = [("raw", None, 0.0)]
    variants += [(f"L1 terms @{th:.2f}", plain_terms, th) for th in thresholds]
    if alias_terms:
        variants += [(f"L1 terms+aliases @{th:.2f}", alias_terms, th) for th in thresholds]
    correctors = {(v, th): TermCorrector(terms, threshold=th) for v, terms, th in variants if terms}

    print("| Hypothesis | Correction | CER raw | CER lenient | Term hits | Replacements |")
    print("|---|---|---|---|---|---|")
    files = sorted(f for d in args.run for f in d.glob("*.txt") if f.name != "reference.txt")
    for f in files:
        if args.only and args.only not in f.name:
            continue
        lines = f.read_text(encoding="utf-8").splitlines()
        for label, terms, th in variants:
            replaced = 0
            if terms:
                out = []
                for line in lines:
                    text, matches = correctors[(label, th)].correct(line)
                    out.append(text)
                    replaced += len(matches)
            else:
                out = lines
            hyp = "".join(out)
            err = mixed_error_rate(reference, hyp)
            lenient = mixed_error_rate(reference, hyp, drop_fillers=True)
            tr = term_recall(reference, hyp, names)
            print(
                f"| `{f.stem}` | {label} | {err.rate:.2%} | {lenient.rate:.2%} | "
                f"{tr.found}/{tr.expected} | {replaced} |"
            )


if __name__ == "__main__":
    main()
