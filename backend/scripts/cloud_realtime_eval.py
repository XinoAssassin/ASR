"""Score DashScope realtime (WebSocket) ASR models on the bake-off chunks (M4 cloud baseline).

    uv run --no-sync python scripts/cloud_realtime_eval.py --run data/bakeoff/part1 [--debug]

Two protocols (Alibaba Model Studio docs, 2026-10):
- duplex ``/api-ws/v1/inference``: ``run-task`` -> binary PCM frames -> ``finish-task``; text in
  ``result-generated`` at ``payload.output.sentence.text`` (``sentence_end`` marks finals).
  Used by Fun-ASR realtime models and qwen-audio-3.1-asr-flash-message.
- OpenAI-Realtime style ``/api-ws/v1/realtime?model=``: ``session.update`` -> base64
  ``input_audio_buffer.append`` -> ``input_audio_buffer.commit``; the input transcript arrives in
  ``conversation.item.input_audio_transcription.completed.transcript``. Used here for
  qwen-audio-3.1-realtime-plus (a speech-to-speech chat model; no response is requested, only the
  transcription of the user's audio is read).

Each chunk is its own short session. Costs money (a few audio-minutes per model).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import websockets
from scipy.signal import resample_poly

from asr.eval.metrics import mixed_error_rate, term_recall
from server.config import get_env

SR = 16_000
FRAME_MS = 100
DUPLEX = {"qwen-audio-3.1-asr-flash-message": SR, "fun-asr-flash-8k-realtime": 8_000}
OPENAI = {"qwen-audio-3.1-realtime-plus"}


def pcm16(x: np.ndarray, rate: int) -> bytes:
    if rate != SR:
        x = resample_poly(x, rate, SR)
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


def ws_base(endpoint: str) -> str:
    return "wss://" + urlparse(endpoint).netloc


async def duplex_chunk(
    base: str, key: str, model: str, audio: bytes, rate: int, debug: bool
) -> str:
    task_id = uuid.uuid4().hex
    header = {"task_id": task_id, "streaming": "duplex"}
    finals: dict[int, str] = {}
    partial: dict[int, str] = {}
    async with websockets.connect(
        base + "/api-ws/v1/inference",
        additional_headers={"Authorization": f"Bearer {key}"},
        open_timeout=30,
        max_size=None,
    ) as ws:
        await ws.send(
            json.dumps(
                {
                    "header": {"action": "run-task", **header},
                    "payload": {
                        "task_group": "audio",
                        "task": "asr",
                        "function": "recognition",
                        "model": model,
                        "parameters": {"format": "pcm", "sample_rate": rate},
                        "input": {},
                    },
                }
            )
        )

        async def sender() -> None:
            step = rate * 2 * FRAME_MS // 1000
            for i in range(0, len(audio), step):
                await ws.send(audio[i : i + step])
                await asyncio.sleep(FRAME_MS / 1000 / 8)  # ~8x faster than real time
            await ws.send(
                json.dumps(
                    {"header": {"action": "finish-task", **header}, "payload": {"input": {}}}
                )
            )

        send_task: asyncio.Task[None] | None = None
        async for raw in ws:
            msg = json.loads(raw)
            event = msg.get("header", {}).get("event")
            if debug:
                print("   <-", event, json.dumps(msg.get("payload", {}), ensure_ascii=False)[:160])
            if event == "task-started":
                send_task = asyncio.create_task(sender())
            elif event == "result-generated":
                sentence = msg["payload"]["output"].get("sentence", {})
                sid = int(sentence.get("sentence_id", len(finals)))
                (finals if sentence.get("sentence_end") else partial)[sid] = sentence.get(
                    "text", ""
                )
            elif event == "task-finished":
                break
            elif event == "task-failed":
                h = msg["header"]
                raise RuntimeError(f"{model}: {h.get('error_code')} {h.get('error_message')}")
        if send_task:
            await send_task
    merged = {**partial, **finals}
    return "".join(merged[k] for k in sorted(merged))


async def openai_chunk(base: str, key: str, model: str, audio: bytes, debug: bool) -> str:
    async with websockets.connect(
        f"{base}/api-ws/v1/realtime?model={model}",
        additional_headers={"Authorization": f"Bearer {key}", "OpenAI-Beta": "realtime=v1"},
        open_timeout=30,
        max_size=None,
    ) as ws:
        session = {
            "modalities": ["text"],
            "input_audio_format": "pcm16",
            "input_audio_transcription": {"language": "zh"},
            "turn_detection": None,
        }
        await ws.send(json.dumps({"type": "session.update", "session": session}))
        step = SR * 2 * FRAME_MS // 1000
        for i in range(0, len(audio), step):
            await ws.send(
                json.dumps(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(audio[i : i + step]).decode(),
                    }
                )
            )
        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=90))
            kind = msg.get("type")
            if debug:
                print("   <-", kind, json.dumps(msg, ensure_ascii=False)[:160])
            if kind == "conversation.item.input_audio_transcription.completed":
                return msg.get("transcript", "")
            if kind == "error":
                raise RuntimeError(f"{model}: {json.dumps(msg.get('error'), ensure_ascii=False)}")
        raise RuntimeError(f"{model}: no transcription event")


async def run_model(base: str, key: str, model: str, chunks: list[np.ndarray], debug: bool):
    texts = []
    for i, x in enumerate(chunks):
        for attempt in range(3):
            try:
                if model in OPENAI:
                    text = await openai_chunk(base, key, model, pcm16(x, SR), debug)
                else:
                    rate = DUPLEX[model]
                    text = await duplex_chunk(base, key, model, pcm16(x, rate), rate, debug)
                break
            except (OSError, websockets.ConnectionClosedError, TimeoutError) as exc:
                if attempt == 2:
                    raise
                print(f"   chunk {i}: {type(exc).__name__}, retrying")
                await asyncio.sleep(2)
        texts.append(text)
        debug = False  # only trace the first chunk
    return texts


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--models", default=",".join([*DUPLEX, *OPENAI]))
    p.add_argument("--debug", action="store_true", help="print the first chunk's server events")
    p.add_argument("--limit", type=int, default=0, help="only the first N chunks (probing)")
    args = p.parse_args()

    env = get_env()
    key = env.dashscope_api_key.get_secret_value()
    base = ws_base(env.dashscope_endpoint)
    prep = np.load(args.run / "chunks.npz")
    chunks = [prep[f"c{i}"] for i in range(int(prep["n"]))]
    if args.limit:
        chunks = chunks[: args.limit]
    reference = (args.run / "reference.txt").read_text(encoding="utf-8").strip()
    terms = json.loads((args.run / "terms.json").read_text(encoding="utf-8"))
    duration = json.loads((args.run / "report.json").read_text(encoding="utf-8"))["excerpt_seconds"]

    rows = []
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        t0 = time.perf_counter()
        try:
            texts = asyncio.run(run_model(base, key, model, chunks, args.debug))
        except Exception as exc:  # report and continue with the next model
            print(f"{model:32} FAILED: {type(exc).__name__}: {str(exc)[:200]}", flush=True)
            rows.append({"model": model, "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
            continue
        seconds = time.perf_counter() - t0
        (args.run / f"cloud-{model}.plain.txt").write_text("\n".join(texts) + "\n", "utf-8")
        hyp = "".join(texts)
        err = mixed_error_rate(reference, hyp)
        lenient = mixed_error_rate(reference, hyp, drop_fillers=True)
        tr = term_recall(reference, hyp, terms)
        row = {
            "model": model,
            "terms": False,
            "cer": round(err.rate, 4),
            "cer_lenient": round(lenient.rate, 4),
            "sub": err.substitutions,
            "del": err.deletions,
            "ins": err.insertions,
            "term_hits": f"{tr.found}/{tr.expected}",
            "per_term": tr.per_term,
            "wall_seconds": round(seconds, 1),
            "wall_rtf": round(seconds / duration, 3),
        }
        rows.append(row)
        print(
            f"{model:32} CER {row['cer']:.2%} (lenient {row['cer_lenient']:.2%})  "
            f"terms {row['term_hits']}  wall RTF {row['wall_rtf']}",
            flush=True,
        )
    (args.run / "cloud_realtime_report.json").write_text(
        json.dumps({"rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
