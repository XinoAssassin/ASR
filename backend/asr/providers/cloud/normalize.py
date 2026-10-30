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

All times are integer milliseconds and are converted to float seconds here. A job may have run as
several remote tasks (one ``ResultTrack`` each, e.g. per-channel D36: channel 0 diarized, every
other channel one speaker); their sentences are merged by time. Speaker ids become
``SPEAKER_{n:02d}``, numbered like the local provider's: track by track, and within a track by
first appearance (per-channel: by channel), only for speakers that have a sentence. Nothing
provider-specific (channel ids, sentence ids, task ids, URLs) reaches the ``Transcript``.
"""

from __future__ import annotations

from collections.abc import Sequence
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
    speaker_key: tuple[int, str, int | None]  # (track, "ch", channel) or (track, "spk", id)
    channel: int
    order: int
    track: int = 0
    words: list[Word] = field(default_factory=list)


@dataclass(frozen=True)
class ResultTrack:
    """The result files of one remote task. ``per_channel``: one speaker per result channel
    (diarization was off); otherwise the sentences' ``speaker_id`` (missing = one speaker)."""

    results: list[dict[str, Any]]
    per_channel: bool


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
    """Normalise the result files of one task (see ``tracks_to_transcript``)."""
    return tracks_to_transcript(
        [ResultTrack(results=results, per_channel=per_channel)],
        language=language,
        fallback_duration=fallback_duration,
        warnings=warnings,
    )


def _sentences(track: ResultTrack, index: int, order: int) -> list[_RawSentence]:
    raw: list[_RawSentence] = []
    for result in track.results:
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
                if track.per_channel:
                    key: tuple[int, str, int | None] = (index, "ch", channel)
                else:
                    key = (index, "spk", _ms(s.get("speaker_id")))
                raw.append(
                    _RawSentence(
                        start_ms=start,
                        end_ms=end,
                        text=text,
                        speaker_key=key,
                        channel=channel,
                        order=order + len(raw),
                        track=index,
                        words=_words(s.get("words"), lo_ms=start, hi_ms=end),
                    )
                )
    return raw


def tracks_to_transcript(
    tracks: Sequence[ResultTrack],
    *,
    language: str | None = None,
    fallback_duration: float | None = None,
    warnings: list[str] | None = None,
) -> Transcript:
    """Merge the result files of one or more tasks into a ``Transcript``."""
    raw: list[_RawSentence] = []
    for index, track in enumerate(tracks):
        raw += _sentences(track, index, len(raw))

    # Stable, contiguous ids: track order, then channel order (per-channel) or first appearance.
    mapping: dict[tuple[int, str, int | None], str] = {}
    for index, track in enumerate(tracks):
        mine = sorted(
            (r for r in raw if r.track == index),
            key=lambda r: (
                (r.channel, r.start_ms, r.order)
                if track.per_channel
                else (r.start_ms, r.end_ms, r.order)
            ),
        )
        for r in mine:
            if r.speaker_key not in mapping:
                mapping[r.speaker_key] = speaker_id(len(mapping))

    raw.sort(key=lambda r: (r.start_ms, r.end_ms, r.track, r.channel, r.order))
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

    duration_ms = result_duration_ms([r for t in tracks for r in t.results])
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
