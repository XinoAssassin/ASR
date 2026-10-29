"""Score DashScope synchronous short-audio ASR models on the bake-off chunks (M4 cloud baseline).

    uv run --no-sync python scripts/cloud_flash_eval.py --run data/bakeoff/part1

Reads chunks.npz / reference.txt / terms.json / report.json written by scripts/bakeoff.py, sends
every chunk (<= 25 s, base64 WAV) to each model on the multimodal-generation endpoint, and prints
CER / lenient CER / term recall per variant. Credentials come from DASHSCOPE_API_KEY /
DASHSCOPE_ENDPOINT (env or .env). Costs money: a 4-minute excerpt is ~4 audio-minutes per variant.

Request shapes, verified 2026-10-29 on cn-beijing.maas.qianwenaiapi.com:
- qwen3-asr-flash: content ``{"audio": data_uri}``, ``parameters.asr_options``; text at
  ``output.choices[0].message.content[0].text``; terms are honoured as a system message.
- qwen-audio-3.x-asr-flash, fun-asr-flash-*: content ``{"type": "input_audio", ...}`` plus
  ``parameters.format`` / ``sample_rate``; text at ``output.sentence.text``; inline vocabulary and
  system-message context are accepted but had no visible effect (hotwords need a vocabulary_id).
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import time
import wave
from pathlib import Path

import httpx
import numpy as np

from asr.eval.metrics import mixed_error_rate, term_recall
from server.config import get_env

SR = 16_000
QWEN3 = "qwen3-asr-flash"


def wav_data_uri(x: np.ndarray) -> str:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
    return "data:audio/wav;base64," + base64.b64encode(buf.getvalue()).decode()


def request_body(model: str, uri: str, context: str | None) -> dict:
    if model == QWEN3:
        messages = [{"role": "user", "content": [{"audio": uri}]}]
        if context:
            messages.insert(0, {"role": "system", "content": [{"text": context}]})
        params: dict = {"asr_options": {"language": "zh", "enable_itn": False}}
        return {"model": model, "input": {"messages": messages}, "parameters": params}
    item = {"type": "input_audio", "input_audio": {"data": uri}}
    body = {
        "model": model,
        "input": {"messages": [{"role": "user", "content": [item]}]},
        "parameters": {"format": "wav", "sample_rate": SR},
    }
    if context:
        body["input"]["context"] = [
            {"role": "user", "content": [{"type": "input_text", "text": context}]}
        ]
    return body


def text_of(payload: dict) -> str:
    out = payload.get("output", {})
    if "choices" in out:
        return out["choices"][0]["message"]["content"][0]["text"]
    if "sentence" in out:
        return out["sentence"].get("text", "")
    if "sentences" in out:
        return "".join(s.get("text", "") for s in out["sentences"])
    raise ValueError(f"unexpected response shape: {list(out)}")


def transcribe(url: str, key: str, model: str, uri: str, context: str | None) -> str:
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "X-DashScope-SSE": "disable",
        "Connection": "close",  # the proxy drops reused connections
    }
    body = request_body(model, uri, context)
    for attempt in range(5):
        try:
            r = httpx.post(url, headers=headers, json=body, timeout=180)
        except (httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.ConnectError):
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 502, 503):
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code != 200:
            raise RuntimeError(f"{model}: HTTP {r.status_code} {r.text[:200]}")
        return text_of(r.json())
    raise RuntimeError(f"{model}: no response after retries")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--run", type=Path, required=True, help="bakeoff output directory")
    p.add_argument(
        "--variants",
        default=f"{QWEN3}:plain,{QWEN3}:terms,qwen-audio-3.1-asr-flash:plain,"
        "qwen-audio-3.1-asr-flash:terms,qwen-audio-3.0-asr-flash:plain,"
        "fun-asr-flash-2026-06-15:plain",
    )
    args = p.parse_args()

    env = get_env()
    key = env.dashscope_api_key.get_secret_value()
    url = env.dashscope_endpoint + "/api/v1/services/aigc/multimodal-generation/generation"
    prep = np.load(args.run / "chunks.npz")
    chunks = [prep[f"c{i}"] for i in range(int(prep["n"]))]
    uris = [wav_data_uri(c) for c in chunks]
    reference = (args.run / "reference.txt").read_text(encoding="utf-8").strip()
    terms = json.loads((args.run / "terms.json").read_text(encoding="utf-8"))
    report = json.loads((args.run / "report.json").read_text(encoding="utf-8"))
    duration = report["excerpt_seconds"]
    context = "术语: " + ", ".join(terms)

    rows = []
    for variant in [v.strip() for v in args.variants.split(",") if v.strip()]:
        model, label = variant.split(":")
        t0 = time.perf_counter()
        texts = [
            transcribe(url, key, model, u, context if label == "terms" else None) for u in uris
        ]
        seconds = time.perf_counter() - t0
        (args.run / f"cloud-{model}.{label}.txt").write_text("\n".join(texts) + "\n", "utf-8")
        hyp = "".join(texts)
        err = mixed_error_rate(reference, hyp)
        lenient = mixed_error_rate(reference, hyp, drop_fillers=True)
        tr = term_recall(reference, hyp, terms)
        row = {
            "model": model,
            "terms": label == "terms",
            "cer": round(err.rate, 4),
            "cer_lenient": round(lenient.rate, 4),
            "sub": err.substitutions,
            "del": err.deletions,
            "ins": err.insertions,
            "term_hits": f"{tr.found}/{tr.expected}",
            "term_recall": round(tr.recall, 3),
            "per_term": tr.per_term,
            "wall_seconds": round(seconds, 1),
            "wall_rtf": round(seconds / duration, 3),
        }
        rows.append(row)
        print(
            f"{model:26} {label:5} CER {row['cer']:.2%} (lenient {row['cer_lenient']:.2%})  "
            f"terms {row['term_hits']}  wall RTF {row['wall_rtf']}",
            flush=True,
        )
    (args.run / "cloud_flash_report.json").write_text(
        json.dumps({"rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
