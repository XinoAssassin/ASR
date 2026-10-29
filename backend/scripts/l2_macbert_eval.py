"""M11 [5b] L2: does generic Chinese spelling correction (MacBERT4CSC) help ASR output?

    uv run --no-sync python scripts/l2_macbert_eval.py --run data/bakeoff/part1 \
        --run data/bakeoff/part3 --model models/hf/shibing624/macbert4csc-base-chinese

MacBERT4CSC (shibing624/macbert4csc-base-chinese, Apache-2.0) is the model behind pycorrector's
``MacBertCorrector``; it is run here directly with transformers (same post-processing: only
CJK characters are replaced, one character for one character), so pycorrector itself is not
needed. For each saved bake-off hypothesis the script reports CER before/after at several
confidence thresholds and classifies every single edit by its effect on the error count against
the reference (helped / hurt / neutral). Hurt edits are false positives.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from asr.eval.metrics import mixed_error_rate, term_recall

MAX_LEN = 126  # BERT 128 minus [CLS]/[SEP]
_PIECE = re.compile(r"[^。！？!?；;，,]*[。！？!?；;，,]?")


def is_cjk(ch: str) -> bool:
    return "一" <= ch <= "鿿"


@dataclass
class Edit:
    line: int
    offset: int
    before: str
    after: str
    prob: float


class MacBert:
    def __init__(self, model_dir: Path) -> None:
        import torch
        from transformers import BertForMaskedLM, BertTokenizerFast

        self.torch = torch
        self.tok = BertTokenizerFast.from_pretrained(str(model_dir))
        self.model = BertForMaskedLM.from_pretrained(str(model_dir)).eval()

    def edits(self, text: str) -> list[tuple[int, str, str, float]]:
        """``(offset, before, after, prob)`` per changed character of ``text``."""
        out: list[tuple[int, str, str, float]] = []
        pieces: list[tuple[int, str]] = []
        buf_start, buf = 0, ""
        for m in _PIECE.finditer(text):
            piece = m.group(0)
            if not piece:
                continue
            if buf and len(buf) + len(piece) > MAX_LEN:
                pieces.append((buf_start, buf))
                buf_start, buf = m.start(), ""
            if not buf:
                buf_start = m.start()
            buf += piece
            while len(buf) > MAX_LEN:
                pieces.append((buf_start, buf[:MAX_LEN]))
                buf_start, buf = buf_start + MAX_LEN, buf[MAX_LEN:]
        if buf:
            pieces.append((buf_start, buf))
        for start, piece in pieces:
            chars = list(piece)
            enc = self.tok(chars, is_split_into_words=True, return_tensors="pt")
            with self.torch.inference_mode():
                logits = self.model(**enc).logits[0]
            probs = logits.softmax(-1)
            word_ids = enc.word_ids()
            for t, w in enumerate(word_ids):
                if w is None:
                    continue
                src = chars[w]
                if not is_cjk(src):
                    continue
                best = int(probs[t].argmax())
                tgt = self.tok.convert_ids_to_tokens(best)
                if len(tgt) != 1 or not is_cjk(tgt) or tgt == src:
                    continue
                out.append((start + w, src, tgt, float(probs[t, best])))
        return out


def apply(lines: list[str], edits: list[Edit]) -> list[str]:
    out = [list(line) for line in lines]
    for e in edits:
        out[e.line][e.offset] = e.after
    return ["".join(chars) for chars in out]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--run", type=Path, action="append", required=True)
    p.add_argument("--model", type=Path, required=True)
    p.add_argument("--thresholds", default="0.0,0.5,0.9,0.99")
    p.add_argument("--only", default="", help="substring filter on hypothesis file names")
    p.add_argument("--dump", type=Path, help="write every edit with its effect as JSON")
    args = p.parse_args()

    corrector = MacBert(args.model)
    thresholds = [float(t) for t in args.thresholds.split(",")]
    dump: list[dict[str, object]] = []
    print(
        "| Hypothesis | Threshold | CER raw | CER after | Lenient raw | Lenient after "
        "| Edits | Helped | Hurt | Neutral | Term hits |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for run in args.run:
        reference = (run / "reference.txt").read_text(encoding="utf-8").strip()
        names = json.loads((run / "terms.json").read_text(encoding="utf-8"))
        for f in sorted(run.glob("*.txt")):
            if f.name == "reference.txt" or "vocabulary" in f.name:
                continue
            if args.only and args.only not in f.name:
                continue
            lines = f.read_text(encoding="utf-8").splitlines()
            t0 = time.perf_counter()
            all_edits = [
                Edit(i, off, b, a, pr)
                for i, line in enumerate(lines)
                for off, b, a, pr in corrector.edits(line)
            ]
            secs = time.perf_counter() - t0
            raw = mixed_error_rate(reference, "".join(lines))
            raw_len = mixed_error_rate(reference, "".join(lines), drop_fillers=True)
            effect: dict[int, int] = {}
            for k, e in enumerate(all_edits):
                one = mixed_error_rate(reference, "".join(apply(lines, [e])))
                effect[k] = (one.substitutions + one.deletions + one.insertions) - (
                    raw.substitutions + raw.deletions + raw.insertions
                )
                dump.append(
                    {
                        "file": f"{run.name}/{f.name}",
                        "line": e.line,
                        "context": lines[e.line][max(e.offset - 8, 0) : e.offset + 8],
                        "before": e.before,
                        "after": e.after,
                        "prob": round(e.prob, 4),
                        "delta_errors": effect[k],
                    }
                )
            for th in thresholds:
                chosen = [k for k, e in enumerate(all_edits) if e.prob >= th]
                hyp = "".join(apply(lines, [all_edits[k] for k in chosen]))
                after = mixed_error_rate(reference, hyp)
                after_len = mixed_error_rate(reference, hyp, drop_fillers=True)
                helped = sum(1 for k in chosen if effect[k] < 0)
                hurt = sum(1 for k in chosen if effect[k] > 0)
                tr_raw = term_recall(reference, "".join(lines), names)
                tr = term_recall(reference, hyp, names)
                print(
                    f"| `{run.name}/{f.stem}` | {th:.2f} | {raw.rate:.2%} | {after.rate:.2%} | "
                    f"{raw_len.rate:.2%} | {after_len.rate:.2%} | {len(chosen)} | {helped} | "
                    f"{hurt} | {len(chosen) - helped - hurt} | {tr_raw.found}->{tr.found} |"
                )
            print(f"<!-- {f.name}: {len(lines)} lines, model time {secs:.1f} s -->")
    if args.dump:
        args.dump.write_text(json.dumps(dump, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
