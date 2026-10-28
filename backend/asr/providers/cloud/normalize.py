"""DashScope filetrans result JSON → core ``Transcript`` (AGENTS.md rule 4).

Result shape (https://help.aliyun.com/zh/model-studio/fun-asr-recorded-speech-recognition-http-api,
"识别结果说明")::

    {"file_url": ..., "properties": {"audio_format", "channels": [0], "original_sampling_rate",
                                     "original_duration_in_milliseconds"},
     "transcripts": [{"channel_id": 0, "content_duration_in_milliseconds", "text",
                      "sentences": [{"begin_time", "end_time", "text", "sentence_id",
                                     "speaker_id"?,  # only with diarization_enabled
                                     "words": [{"begin_time", "end_time", "text",
                                                "punctuation"}]}]}]}

All times are integer milliseconds and are converted to float seconds here. Speaker ids become
``SPEAKER_{n:02d}`` numbered by first appearance; in per-channel mode each channel is one
speaker. Nothing provider-specific (channel ids, sentence ids, URLs) reaches the ``Transcript``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from asr.providers.base import (
    Segment,
    Transcript,
    Word,
    make_speakers,
    speaker_id,
)


@dataclass(frozen=True)
class _RawSentence:
    start_ms: int
    end_ms: int
    text: str
    speaker_key: tuple[str, int] | None  # ("ch", channel) or ("spk", diarized id)
    channel: int
    order: int
    words: list[Word] = field(default_factory=list)


def _ms(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return max(int(value), 0)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return max(int(value), 0)
    return None


def _seconds(ms: int) -> float:
    return round(ms / 1000.0, 3)


def _words(raw: Any, *, lo_ms: int, hi_ms: int) -> list[Word]:
    words: list[Word] = []
    if not isinstance(raw, list):
        return words
    for w in raw:
        if not isinstance(w, dict):
            continue
        start, end = _ms(w.get("begin_time")), _ms(w.get("end_time"))
        if start is None or end is None:
            continue
        text = f"{w.get('text') or ''}{w.get('punctuation') or ''}".strip()
        if not text:
            continue
        start = min(max(start, lo_ms), hi_ms)
        end = min(max(end, start), hi_ms)
        words.append(Word(text=text, start=_seconds(start), end=_seconds(end)))
    return words


def result_duration_ms(results: list[dict[str, Any]]) -> int | None:
    """Largest ``properties.original_duration_in_milliseconds`` among the result files."""
    best: int | None = None
    for result in results:
        props = result.get("properties")
        if isinstance(props, dict):
            ms = _ms(props.get("original_duration_in_milliseconds"))
            if ms is not None and (best is None or ms > best):
                best = ms
    return best


def to_transcript(
    results: list[dict[str, Any]],
    *,
    per_channel: bool,
    language: str | None = None,
    fallback_duration: float | None = None,
    warnings: list[str] | None = None,
) -> Transcript:
    """Normalise one or more result files into a ``Transcript``.

    ``per_channel``: one speaker per transcript channel (diarization was off). Otherwise the
    sentence ``speaker_id`` is used; sentences without one (diarization off or a single
    speaker) fall back to ``SPEAKER_00``.
    """
    raw: list[_RawSentence] = []
    order = 0
    for result in results:
        transcripts = result.get("transcripts")
        if not isinstance(transcripts, list):
            continue
        for tr in transcripts:
            if not isinstance(tr, dict):
                continue
            channel = _ms(tr.get("channel_id")) or 0
            sentences = tr.get("sentences")
            if not isinstance(sentences, list):
                continue
            for s in sentences:
                if not isinstance(s, dict):
                    continue
                text = str(s.get("text") or "").strip()
                start, end = _ms(s.get("begin_time")), _ms(s.get("end_time"))
                if not text or start is None or end is None:
                    continue
                end = max(end, start)
                if per_channel:
                    key: tuple[str, int] | None = ("ch", channel)
                else:
                    spk = _ms(s.get("speaker_id"))
                    key = ("spk", spk) if spk is not None else None
                raw.append(
                    _RawSentence(
                        start_ms=start,
                        end_ms=end,
                        text=text,
                        speaker_key=key,
                        channel=channel,
                        order=order,
                        words=_words(s.get("words"), lo_ms=start, hi_ms=end),
                    )
                )
                order += 1

    raw.sort(key=lambda r: (r.start_ms, r.end_ms, r.channel, r.order))

    # Stable, contiguous speaker ids. Per-channel: channel order; otherwise first appearance.
    mapping: dict[tuple[str, int] | None, str] = {}
    if per_channel:
        for n, ch in enumerate(sorted({r.channel for r in raw})):
            mapping[("ch", ch)] = speaker_id(n)
    for r in raw:
        if r.speaker_key not in mapping:
            mapping[r.speaker_key] = speaker_id(len(mapping))

    segments = [
        Segment(
            id=f"seg-{i:05d}",
            speaker_id=mapping[r.speaker_key],
            start=_seconds(r.start_ms),
            end=_seconds(r.end_ms),
            text=r.text,
            words=r.words,
        )
        for i, r in enumerate(raw)
    ]

    duration_ms = result_duration_ms(results)
    duration = _seconds(duration_ms) if duration_ms is not None else fallback_duration
    if segments:
        last_end = max(s.end for s in segments)
        duration = last_end if duration is None else max(duration, last_end)

    return Transcript(
        provider="cloud",
        duration=duration,
        language=language,
        speakers=make_speakers(sorted({s.speaker_id for s in segments})),
        segments=segments,
        warnings=list(warnings or []),
    )
