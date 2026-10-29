"""HTTP API models (EXECUTION_LOG §3.5, PLAN §5). Frozen contract.

Core types (``JobOptions``, ``Term``, ``Speaker``, ``Segment``, ``ProbeInfo``,
``ConnectionTestResult``) are reused from ``asr``, never duplicated. The frontend TypeScript client
is generated from the OpenAPI document built from these models.
"""

from datetime import datetime
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from asr.audio.types import ProbeInfo
from asr.providers.base import (
    ConnectionTestResult,
    Correction,
    FileTransferMode,
    JobOptions,
    ProviderName,
    Segment,
    Speaker,
    Term,
)
from asr.writers import ExportFormat

__all__ = [
    "JOB_STAGES",
    "MAX_UPLOAD_SIZE",
    "CloudSettingsOut",
    "CloudSettingsUpdate",
    "ConnectionTestResult",
    "Device",
    "EditTextOp",
    "ExportFormat",
    "FsDir",
    "FsDirsOut",
    "FsRoot",
    "HealthOut",
    "JobCreate",
    "JobList",
    "JobOut",
    "JobStatus",
    "LocalModelOut",
    "LocalSettingsOut",
    "LocalSettingsUpdate",
    "SetSpeakerOp",
    "SettingsOut",
    "SettingsUpdate",
    "SpeakerUpdate",
    "SpeakersUpdate",
    "TermSetImport",
    "TermSetIn",
    "TermSetOut",
    "TranscriptOp",
    "TranscriptOut",
    "TranscriptPatch",
    "UploadInitIn",
    "UploadInitOut",
    "UploadOut",
    "UploadPartOut",
    "UploadRegisterIn",
]

MAX_UPLOAD_SIZE = 2 * 1024**3  # 2 GiB (PLAN §5.1)

# EXECUTION_LOG §3.4. ``JobOut.stage`` is one of these; unknown keys must still render in the UI.
JOB_STAGES: tuple[str, ...] = (
    "queued",
    "preparing",
    "uploading",
    "cloud_queued",
    "recognizing",
    "finalizing",
    "done",
    # reserved for M5 (local pipeline)
    "decoding",
    "vad",
    "asr",
    "aligning",
    "diarizing",
    "merging",
)

JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
Device = Literal["cpu", "cuda"]


# ---------------------------------------------------------------------------------------- health
class HealthOut(BaseModel):
    status: Literal["ok"] = "ok"
    version: str
    api_key_required: bool
    decoder: Literal["pyav", "imageio"]
    ffmpeg_version: str | None = Field(description="None if the bundled FFmpeg failed to load.")


# --------------------------------------------------------------------------------------- uploads
class UploadOut(BaseModel):
    id: str
    path: str = Field(description="Absolute server-side path of the stored file.")
    original_name: str
    size: int = Field(ge=0, description="Bytes.")
    created_at: datetime
    registered: bool = Field(
        default=False,
        description="Registered from an existing server file; never deleted by the backend.",
    )
    probe: ProbeInfo | None = None


class UploadInitIn(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0, description="Total bytes; over MAX_UPLOAD_SIZE is rejected with 413.")
    target_dir: str = Field(
        max_length=4096,
        description="Absolute path, or relative to the first upload root; must stay inside a root.",
    )


class UploadInitOut(BaseModel):
    session_id: str
    part_size: int = Field(gt=0, description="Bytes per part; the last part may be shorter.")
    total_parts: int = Field(ge=1)


class UploadPartOut(BaseModel):
    n: int = Field(ge=0)
    size: int = Field(ge=0, description="Bytes received for this part.")


class UploadRegisterIn(BaseModel):
    path: str = Field(
        min_length=1, max_length=4096, description="Existing server file inside an upload root."
    )


# -------------------------------------------------------------------------------------------- fs
class FsRoot(BaseModel):
    name: str
    path: str


class FsDir(BaseModel):
    name: str
    path: str


class FsDirsOut(BaseModel):
    path: str
    parent: str | None = Field(description="None at a root (never escapes the root).")
    root: str
    dirs: list[FsDir]


# ------------------------------------------------------------------------------------------ jobs
class JobCreate(BaseModel):
    """Exactly one of ``upload_id`` / ``file_url``.

    Effective provider: ``provider`` if given, else ``options.provider`` if the client set it,
    else the settings default; the server stores it in ``options.provider``. ``local`` jobs need an
    upload and downloaded models (422 otherwise); the resolved ASR model and device are pinned in
    ``options.local``. ``file_url`` jobs are cloud-only (D3). ``options.terms`` is rebuilt
    server-side from ``term_set_ids`` + ``extra_terms``.
    """

    upload_id: str | None = None
    file_url: str | None = Field(
        default=None,
        max_length=4096,
        description="Public http(s) URL fetched by DashScope, never by this backend.",
    )
    provider: ProviderName | None = None
    options: JobOptions = Field(default_factory=JobOptions)
    term_set_ids: list[str] = Field(default_factory=list, max_length=50)
    extra_terms: list[Term] = Field(default_factory=list, max_length=1000)

    @field_validator("file_url")
    @classmethod
    def _check_url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        parts = urlsplit(v)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("file_url must be an absolute http(s) URL")
        return v

    @model_validator(mode="after")
    def _exactly_one_source(self) -> Self:
        if (self.upload_id is None) == (self.file_url is None):
            raise ValueError("exactly one of upload_id / file_url is required")
        return self


class JobOut(BaseModel):
    id: str
    upload_id: str | None
    file_url: str | None
    provider: ProviderName
    options: JobOptions
    status: JobStatus
    stage: str = Field(description="Stage key, see JOB_STAGES / EXECUTION_LOG §3.4.")
    progress: float = Field(ge=0, le=1)
    message: str | None = None
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    cloud_task_id: str | None = None
    transcript_id: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class JobList(BaseModel):
    items: list[JobOut]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)


# ----------------------------------------------------------------------------------- transcripts
class TranscriptOut(BaseModel):
    id: str
    job_id: str
    provider: ProviderName
    duration: float | None = None
    language: str | None = None
    speakers: list[Speaker]
    segments: list[Segment] = Field(description="Current (possibly edited) segments.")
    original: list[Segment] = Field(description="Immutable recognition result (rule 9).")
    warnings: list[str] = Field(default_factory=list)
    corrections: list[Correction] = Field(
        default_factory=list,
        description="[5b] automatic edits that turned ``original`` into ``segments``.",
    )
    created_at: datetime
    updated_at: datetime


class EditTextOp(BaseModel):
    op: Literal["edit_text"]
    segment_id: str
    text: str = Field(max_length=10000)


class SetSpeakerOp(BaseModel):
    op: Literal["set_speaker"]
    segment_id: str
    speaker_id: str = Field(min_length=1, max_length=100)


# ``split`` / ``merge_segments`` join this union in M7 (D14).
TranscriptOp = Annotated[EditTextOp | SetSpeakerOp, Field(discriminator="op")]


class TranscriptPatch(BaseModel):
    ops: list[TranscriptOp] = Field(min_length=1, max_length=1000)


class SpeakerUpdate(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")


class SpeakersUpdate(BaseModel):
    speakers: list[SpeakerUpdate] = Field(max_length=100)


# ------------------------------------------------------------------------------------- term sets
class TermSetIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    terms: list[Term] = Field(default_factory=list, max_length=1000)


class TermSetImport(BaseModel):
    """Plain-text import: one term per line, optionally ``term<TAB or ,>weight[<TAB or ,>note]``."""

    name: str = Field(min_length=1, max_length=100)
    text: str = Field(max_length=200_000)


class TermSetOut(BaseModel):
    id: str
    name: str
    terms: list[Term]
    created_at: datetime
    updated_at: datetime


# -------------------------------------------------------------------------------------- settings
class LocalSettingsOut(BaseModel):
    asr_model: str | None
    diarize_model: str | None
    device: Device
    model_cache_dir: str


class CloudSettingsOut(BaseModel):
    endpoint: str
    model: str
    file_transfer: FileTransferMode
    api_key_set: bool
    api_key_masked: str | None = Field(description="E.g. 'sk-****abcd'; never the full key.")


class SettingsOut(BaseModel):
    provider_default: ProviderName
    api_key_required: bool
    upload_roots: list[str]
    local: LocalSettingsOut
    cloud: CloudSettingsOut


class LocalSettingsUpdate(BaseModel):
    asr_model: str | None = Field(default=None, max_length=200)
    diarize_model: str | None = Field(default=None, max_length=200)
    device: Device | None = None


class CloudSettingsUpdate(BaseModel):
    endpoint: str | None = Field(
        default=None, max_length=200, description="Must be an allowed DashScope endpoint (D2)."
    )
    model: str | None = Field(default=None, min_length=1, max_length=200)
    file_transfer: FileTransferMode | None = None
    api_key: SecretStr | None = Field(
        default=None,
        max_length=500,
        description="Write-only. Non-empty sets the override, '' clears it, omitted leaves it.",
    )


class SettingsUpdate(BaseModel):
    """Partial update: omitted fields are unchanged. Env-only settings are not updatable (D7)."""

    provider_default: ProviderName | None = None
    local: LocalSettingsUpdate | None = None
    cloud: CloudSettingsUpdate | None = None


class LocalModelOut(BaseModel):
    id: str = Field(description="Registry key, as used by `asr models download <id>`.")
    name: str = Field(description="Hub repository id.")
    kind: Literal["asr", "aligner", "vad", "diarization"]
    size_mb: int | None = None
    downloaded: bool
    status: Literal["missing", "downloading", "downloaded", "failed"] = "missing"
    error: str | None = None
    has_mirror: bool = Field(default=False, description="Also available on ModelScope.")
