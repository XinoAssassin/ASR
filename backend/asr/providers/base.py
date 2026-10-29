"""Provider contract and core transcript types (EXECUTION_LOG §3.1). Frozen contract.

Every provider returns the same ``Transcript`` shape; provider-specific data must not leak into it
(AGENTS.md rule 4). Timestamps are seconds (float) throughout. Pydantic v2 only, no web imports.
"""

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Literal, Protocol, Self, runtime_checkable

from pydantic import BaseModel, Field, field_validator, model_validator

from asr.audio.types import ProbeInfo

ProviderName = Literal["local", "cloud"]
FileTransferMode = Literal["temp_upload", "public_url"]
"""How the cloud provider obtains an audio URL (docs/research/file-transfer-for-cloud-asr.md).
``oss`` (PLAN §4.4 b) is not implemented in M0-M3."""

# Twelve distinguishable colours, assigned to speakers in order of first appearance.
SPEAKER_COLORS: tuple[str, ...] = (
    "#1f77b4",
    "#ff7f0e",
    "#2ca02c",
    "#d62728",
    "#9467bd",
    "#8c564b",
    "#e377c2",
    "#17becf",
    "#bcbd22",
    "#7f7f7f",
    "#393b79",
    "#ad494a",
)


# ------------------------------------------------------------------------------------ transcript
class Word(BaseModel):
    text: str
    start: float = Field(ge=0, description="Seconds.")
    end: float = Field(ge=0, description="Seconds.")


class Segment(BaseModel):
    id: str
    speaker_id: str = Field(description="Opaque speaker id, e.g. 'SPEAKER_00'.")
    start: float = Field(ge=0, description="Seconds.")
    end: float = Field(ge=0, description="Seconds.")
    text: str
    words: list[Word] = Field(default_factory=list)
    edited: bool = False


class Speaker(BaseModel):
    id: str
    name: str
    color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")


class Transcript(BaseModel):
    provider: ProviderName
    duration: float | None = Field(default=None, ge=0, description="Seconds.")
    language: str | None = None
    speakers: list[Speaker] = Field(default_factory=list)
    segments: list[Segment] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def speaker_id(n: int) -> str:
    """``3 -> "SPEAKER_03"``."""
    if n < 0:
        raise ValueError("speaker index must be >= 0")
    return f"SPEAKER_{n:02d}"


def make_speakers(speaker_ids: Iterable[str]) -> list[Speaker]:
    """Build the speaker list in first-appearance order; ``name`` starts equal to ``id``."""
    seen: dict[str, None] = dict.fromkeys(speaker_ids)
    return [
        Speaker(id=sid, name=sid, color=SPEAKER_COLORS[i % len(SPEAKER_COLORS)])
        for i, sid in enumerate(seen)
    ]


# ---------------------------------------------------------------------------------- job options
BOOSTED_TERM_WEIGHT = 50


class Term(BaseModel):
    term: str = Field(min_length=1, max_length=100)
    weight: int = Field(
        default=3, description="1..5 for normal terms, 50 for boosted terms (cloud vocabulary)."
    )
    note: str | None = Field(default=None, max_length=500)
    aliases: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Known mis-hearings, replaced by the term in post-correction ([5b] L1).",
    )
    blacklist: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Words that block a replacement when they appear next to the match.",
    )

    @field_validator("aliases", "blacklist")
    @classmethod
    def _clean_words(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for word in v:
            word = word.strip()
            if len(word) > 100:
                raise ValueError("aliases / blacklist entries are at most 100 characters")
            if word and word not in out:
                out.append(word)
        return out

    @field_validator("term")
    @classmethod
    def _strip_term(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("term must not be blank")
        return v

    @field_validator("weight")
    @classmethod
    def _check_weight(cls, v: int) -> int:
        if not (1 <= v <= 5 or v == BOOSTED_TERM_WEIGHT):
            raise ValueError(f"weight must be 1..5 or {BOOSTED_TERM_WEIGHT}")
        return v


ChannelsOption = Literal["auto", "downmix", "left", "right", "per_channel"]


class PreprocessOptions(BaseModel):
    channels: ChannelsOption = "auto"
    stream_index: int = Field(default=0, ge=0, description="Audio stream among the container's.")
    peak_gain: bool = Field(default=True, description="[0d] peak-safety gain (local pipeline).")
    # M6 extension point: denoise / vocal_separation / dereverb / loudness toggles.


class LocalOptions(BaseModel):
    asr_model: str | None = None
    diarize_model: str | None = None
    device: Literal["cpu", "cuda"] | None = None


class CloudOptions(BaseModel):
    model: str | None = None
    keep_dialect: bool = False


class CorrectionOptions(BaseModel):
    """[5b] post-correction toggles (docs/research/post-correction.md), for every provider."""

    terms: bool = Field(default=True, description="L1 phoneme-level term replacement.")
    repetition: bool = Field(default=True, description="Collapse runaway repetitions.")
    threshold: float = Field(
        default=0.85, ge=0.5, le=1.0, description="Phoneme similarity needed to replace."
    )


class Correction(BaseModel):
    """One automatic edit of a segment's text (raw text stays in ``original``)."""

    segment_id: str
    source: Literal["term", "repetition"]
    before: str
    after: str
    score: float | None = None


class JobOptions(BaseModel):
    provider: ProviderName = "cloud"
    language_hints: list[str] = Field(default_factory=lambda: ["zh", "en"], max_length=10)
    num_speakers: int | None = Field(default=None, ge=1, le=20, description="None = auto.")
    terms: list[Term] = Field(
        default_factory=list,
        description="Expanded server-side from term_set_ids + extra_terms.",
    )
    context: str | None = Field(default=None, max_length=2000)
    preprocess: PreprocessOptions = Field(default_factory=PreprocessOptions)
    local: LocalOptions = Field(default_factory=LocalOptions)
    cloud: CloudOptions = Field(default_factory=CloudOptions)
    correction: CorrectionOptions = Field(default_factory=CorrectionOptions)


# ------------------------------------------------------------------------------ provider input
class AudioSource(BaseModel):
    """Exactly one of ``path`` / ``url``."""

    path: Path | None = None
    url: str | None = Field(
        default=None,
        description="Public http(s) URL; cloud provider only, the backend never fetches it.",
    )
    probe: ProbeInfo | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.path is None) == (self.url is None):
            raise ValueError("exactly one of path / url is required")
        return self


class ProgressEvent(BaseModel):
    stage: str = Field(description="Job stage key, EXECUTION_LOG §3.4.")
    progress: float = Field(ge=0, le=1, description="Overall progress 0..1.")
    message: str | None = None
    external_id: str | None = Field(
        default=None, description="E.g. the DashScope task_id; stored as Job.cloud_task_id."
    )


ProgressCallback = Callable[[ProgressEvent], None]
"""Called by providers at least once per poll interval; may raise ``JobCancelled``."""


class JobCancelled(Exception):
    """Raised by the progress callback to abort a running provider."""


class ProviderError(Exception):
    """Provider failure. The message is user-facing and must not contain secrets."""


class ConnectionTestResult(BaseModel):
    """Result of a cloud credential check (re-exported by ``asr.providers.cloud``)."""

    ok: bool
    message: str
    max_file_size_mb: int | None = None


@runtime_checkable
class TranscriptionProvider(Protocol):
    """Synchronous provider; the job runner calls it in a worker thread.

    Cancellation: the ``progress`` callback raises ``JobCancelled``.
    """

    name: ProviderName

    def transcribe(
        self, source: AudioSource, options: JobOptions, progress: ProgressCallback
    ) -> Transcript: ...
