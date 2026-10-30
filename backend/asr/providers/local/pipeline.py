"""Local provider: [0] decode -> [1] VAD -> [2] ASR -> [3] align -> [4] diarize -> [5] merge.

Channel policy (PLAN §4.5 [0b]): mono / downmix / a selected channel give one track; ``per_channel``
gives one track per channel, where channel 0 is diarized (remote side, possibly several people) and
every other channel is one known speaker (local mic, the other phone party). Models load lazily and
are cached per process; jobs run one at a time on the local worker.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from asr.audio import channels as audio_channels
from asr.audio import decode as audio_decode
from asr.audio.preprocess import clipping_ratio, peak_safety_gain
from asr.providers.base import (
    AudioSource,
    JobOptions,
    ProgressCallback,
    ProgressEvent,
    ProviderError,
    Transcript,
    make_speakers,
    speaker_id,
)
from asr.providers.languages import normalize_hints
from asr.providers.local.device import (
    DeviceError,
    cpu_threads,
    disable_broken_rocm_kernels,
    resolve_dtype,
    torch_device,
    whisper_device,
)
from asr.providers.local.merge import ChunkResult, SpeakerTurn, Token, build_segments
from asr.providers.local.models import ModelNotDownloaded, engine_installed, spec
from asr.providers.local.vad import SAMPLE_RATE, cut, merge_segments

MAX_CHUNK_S = 25.0
CLIPPING_WARN_RATIO = 1e-3

log = logging.getLogger(__name__)


# ------------------------------------------------------------------------------- engine protocols
class Vad(Protocol):
    def segments(self, audio: np.ndarray) -> list[tuple[float, float]]: ...


class AsrEngine(Protocol):
    def transcribe(
        self,
        chunks: Sequence[np.ndarray],
        *,
        language: str | None = ...,
        terms: Sequence[str] = ...,
        context: str | None = ...,
    ) -> list[str]: ...


class Aligner(Protocol):
    def align(self, audio: np.ndarray, text: str, *, language: str = ...) -> list[Any]: ...


class Diarizer(Protocol):
    def diarize(
        self, audio: np.ndarray, speech: Sequence[tuple[float, float]], *, num_speakers: int | None
    ) -> list[Any]: ...


@dataclass
class LocalEngines:
    vad: Vad
    asr: AsrEngine
    aligner: Aligner | None  # None when the ASR engine emits its own timestamps (Paraformer)
    diarizer: Diarizer


def default_asr_model(device: str = "cpu") -> str:
    """M4 decision (docs/M4_BAKEOFF.md): 1.7B where bf16 is native, 0.6B otherwise.

    ``device`` is the configured one (``cpu`` | ``cuda`` | ``rocm``); GPUs use torch.cuda's answer.
    """
    return (
        "qwen3-asr-1.7b" if resolve_dtype(None, device=device) == "bfloat16" else "qwen3-asr-0.6b"
    )


def configure_threads() -> int:
    """CPU inference threads: ``ASR_TORCH_THREADS``, default min(16, CPU count) (M4 setting)."""
    import torch

    threads = cpu_threads()
    torch.set_num_threads(threads)
    return threads


_ENGINE_CACHE: dict[tuple[str, str, str, str], LocalEngines] = {}
_ENGINE_LOCK = threading.Lock()


def load_engines(cache_dir: Path, asr_model: str, *, device: str, dtype: str) -> LocalEngines:
    """Load (or reuse) the models for one ASR choice. Never downloads (AGENTS rule 6).

    ``device`` is the configured one; ``rocm`` runs on torch's ``cuda`` device (``torch_device``).
    """
    key = (str(cache_dir), asr_model, device, dtype)
    with _ENGINE_LOCK:
        if key in _ENGINE_CACHE:
            return _ENGINE_CACHE[key]
        try:
            run_on = torch_device(device)
        except DeviceError as exc:
            raise ProviderError(str(exc)) from None
        if run_on == "cpu":
            configure_threads()
        else:
            import torch

            log.info("local models on %s: %s", device, torch.cuda.get_device_name(0))
            disabled = disable_broken_rocm_kernels()
            if disabled:
                log.warning(
                    "%s do not run on this GPU; using torch's generic kernels instead",
                    ", ".join(disabled),
                )
        try:
            s = spec(asr_model)
        except KeyError as exc:
            raise ProviderError(str(exc.args[0])) from None
        if s.kind != "asr":
            raise ProviderError(f"{asr_model!r} is not an ASR model")
        # Keep one model set in memory at a time: release the old one *before* loading the new
        # one, so a swap never holds two sets at peak.
        _ENGINE_CACHE.clear()
        try:
            from asr.providers.local.diar_campplus import CamPlusDiarizer
            from asr.providers.local.vad import FsmnVad

            if s.engine == "funasr":
                from asr.providers.local.asr_paraformer import ParaformerAsr

                asr: Any = ParaformerAsr(cache_dir, device=run_on)
                aligner: Aligner | None = None
            elif s.engine == "faster_whisper":
                if not engine_installed(asr_model):
                    raise ProviderError(
                        f"{asr_model} needs the faster-whisper package "
                        "(install the backend with the `whisper` extra)"
                    )
                from asr.providers.local.asr_whisper import WhisperAsr

                whisper_on, fallback = whisper_device(device)
                if fallback:
                    log.warning("%s", fallback)
                asr = WhisperAsr(cache_dir, asr_model, device=whisper_on, dtype=dtype)
                aligner = None
            else:
                from asr.providers.local.align import ForcedAligner
                from asr.providers.local.asr_transformers import TransformersAsr

                asr = TransformersAsr(cache_dir, asr_model, device=run_on, dtype=dtype)
                aligner = ForcedAligner(cache_dir, device=run_on, dtype=dtype)
            engines = LocalEngines(
                vad=FsmnVad(cache_dir, device=run_on),
                asr=asr,
                aligner=aligner,
                diarizer=CamPlusDiarizer(cache_dir, device=run_on),
            )
        except ModelNotDownloaded as exc:
            raise ProviderError(str(exc)) from None
        _ENGINE_CACHE[key] = engines
        return engines


# --------------------------------------------------------------------------------------- provider
@dataclass
class _Track:
    channel: int | None  # None = downmix of all channels
    diarize: bool
    audio: np.ndarray


class LocalPipelineProvider:
    name = "local"

    def __init__(
        self,
        *,
        cache_dir: Path,
        asr_model: str | None = None,
        device: str = "cpu",
        dtype: str = "auto",
        engines: LocalEngines | None = None,
        max_chunk: float = MAX_CHUNK_S,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.device = device
        self.dtype = dtype
        self.asr_model = asr_model or default_asr_model(device)
        self._engines = engines
        self.max_chunk = max_chunk

    def transcribe(
        self, source: AudioSource, options: JobOptions, progress: ProgressCallback
    ) -> Transcript:
        if source.path is None:
            raise ProviderError("The local provider needs an uploaded file, not a URL")
        warnings: list[str] = []

        def report(stage: str, value: float, message: str | None = None) -> None:
            progress(
                ProgressEvent(stage=stage, progress=min(max(value, 0.0), 1.0), message=message)
            )

        report("decoding", 0.01, "Decoding audio")
        tracks, duration = self._decode(Path(source.path), source, options, warnings)
        engines = self._engines or load_engines(
            self.cache_dir, self.asr_model, device=self.device, dtype=self.dtype
        )
        terms = [t.term for t in options.terms]
        language = _language(options.language_hints)
        if _is_whisper(self.asr_model) and self.device != "cpu":
            _, fallback = whisper_device(self.device)
            if fallback:
                warnings.append(fallback)
        if _is_whisper(self.asr_model) and language != "en":
            warnings.append(
                f"{self.asr_model} is the English-only route; set language_hints to ['en'] "
                "or use a Qwen3-ASR model for Chinese audio"
            )
        single_speaker = options.num_speakers == 1
        # Every non-diarized track (per_channel channels 1..n) is one known speaker, so the
        # diarized track holds the job's speaker count minus those.
        fixed_tracks = sum(1 for t in tracks if not t.diarize)
        oracle = options.num_speakers - fixed_tracks if options.num_speakers is not None else None

        all_segments = []
        next_label = 0
        share = 0.95 / len(tracks)
        for index, track in enumerate(tracks):
            base = 0.03 + index * share

            def at(fraction: float, base: float = base) -> float:
                return base + fraction * share

            report("vad", at(0.0), "Detecting speech")
            speech = engines.vad.segments(track.audio)
            spans = merge_segments(speech, max_len=self.max_chunk, audio=track.audio)
            pieces = cut(track.audio, spans)

            chunks = self._recognise(
                engines, pieces, spans, terms, options.context, language, report, at, warnings
            )

            # Track-local labels first; real ids are handed out below, only to speakers that
            # end up with text, so a silent channel or an unused cluster uses up no id.
            diarize = track.diarize and not single_speaker and (oracle is None or oracle > 1)
            if diarize and speech:
                report("diarizing", at(0.85), "Identifying speakers")
                raw_turns = engines.diarizer.diarize(track.audio, speech, num_speakers=oracle)
                turns = [SpeakerTurn(t.start, t.end, f"t{t.label}") for t in raw_turns]
                fallback = turns[0].speaker if turns else "t"
            else:
                for c in chunks:
                    c.speaker = "t"
                turns, fallback = [], "t"

            report("merging", at(0.97), "Building transcript")
            track_segments = build_segments(chunks, turns, default_speaker=fallback)
            names: dict[str, str] = {}
            for seg in sorted(track_segments, key=lambda s: (s.start, s.end)):
                if seg.speaker_id not in names:
                    names[seg.speaker_id] = speaker_id(next_label + len(names))
            next_label += len(names)
            all_segments.extend(
                s.model_copy(update={"speaker_id": names[s.speaker_id]}) for s in track_segments
            )

        all_segments.sort(key=lambda s: (s.start, s.end))
        segments = [s.model_copy(update={"id": f"seg_{i:05d}"}) for i, s in enumerate(all_segments)]
        report("merging", 1.0, "Done")
        return Transcript(
            provider="local",
            duration=duration,
            language=None,
            speakers=make_speakers(s.speaker_id for s in segments),
            segments=segments,
            warnings=warnings,
        )

    # ---------------------------------------------------------------------------------- stages
    def _decode(
        self, path: Path, source: AudioSource, options: JobOptions, warnings: list[str]
    ) -> tuple[list[_Track], float]:
        stream = options.preprocess.stream_index
        try:
            probe = source.probe
            if probe is None or probe.stream_index != stream:  # stored probe is of stream 0
                probe = audio_decode.probe(path, stream)
            resolved = audio_channels.resolve_channels(probe, options.preprocess.channels)
        except ValueError as exc:
            raise ProviderError(f"Cannot read the audio file: {exc}") from None
        if resolved.mode == "per_channel":
            plan = [(ch, i == 0) for i, ch in enumerate(resolved.channel_ids)]
        elif resolved.mode == "select":
            plan = [(resolved.channel_ids[0], True)]
        else:
            plan = [(None, True)]
        tracks = []
        for channel, diarize in plan:
            try:
                audio = audio_decode.decode_pcm16k(path, channel=channel, stream_index=stream)
            except ValueError as exc:
                raise ProviderError(f"Cannot decode the audio file: {exc}") from None
            ratio = clipping_ratio(audio)
            if ratio > CLIPPING_WARN_RATIO:
                where = "" if channel is None else f" (channel {channel})"
                warnings.append(f"Audio clips on {ratio:.2%} of samples{where}")
            if options.preprocess.peak_gain:
                audio, _ = peak_safety_gain(audio)
            tracks.append(_Track(channel=channel, diarize=diarize, audio=audio))
        duration = max((len(t.audio) for t in tracks), default=0) / SAMPLE_RATE
        return tracks, duration

    def _recognise(
        self,
        engines: LocalEngines,
        pieces: list[np.ndarray],
        spans: list[tuple[float, float]],
        terms: list[str],
        context: str | None,
        language: str | None,
        report: Callable[[str, float, str | None], None],
        at: Callable[[float], float],
        warnings: list[str],
    ) -> list[ChunkResult]:
        chunks: list[ChunkResult] = []
        timed = getattr(engines.asr, "transcribe_timed", None)
        n = max(len(pieces), 1)
        for i, (piece, (start, end)) in enumerate(zip(pieces, spans, strict=True)):
            report("asr", at(0.05 + 0.6 * i / n), f"Recognising speech ({i + 1}/{n})")
            if timed is not None:
                text, raw = timed([piece], terms=terms, language=language)[0]
                tokens = [Token(t, start + a, start + b) for t, a, b in raw]
            else:
                text = engines.asr.transcribe(
                    [piece], language=language, terms=terms, context=context
                )[0]
                tokens = []
                if engines.aligner is not None and text.strip():
                    report("aligning", at(0.05 + 0.6 * (i + 0.5) / n), f"Aligning ({i + 1}/{n})")
                    try:
                        aligned = engines.aligner.align(piece, text, language=language or "zh")
                    except (ValueError, ImportError) as exc:
                        # An unsupported language (ValueError) or a missing tokenizer package
                        # (ImportError: nagisa for ja, soynlp for ko): keep the text, no words.
                        if not any(w.startswith("Word timestamps unavailable") for w in warnings):
                            warnings.append(f"Word timestamps unavailable: {exc}")
                        aligned = []
                    tokens = [Token(t.text, start + t.start, start + t.end) for t in aligned]
            chunks.append(ChunkResult(start=start, end=end, text=text, tokens=tokens))
        return chunks


def _is_whisper(asr_model: str) -> bool:
    try:
        return spec(asr_model).engine == "faster_whisper"
    except KeyError:
        return False


def _language(hints: Sequence[str]) -> str | None:
    """Chinese-primary: force zh when it is among the hints, else the first known hint, else
    auto-detect (``None``). Hints are normalised; ``auto`` and unknown codes force nothing."""
    cleaned = normalize_hints(hints)
    if "zh" in cleaned:
        return "zh"
    return cleaned[0] if cleaned else None
