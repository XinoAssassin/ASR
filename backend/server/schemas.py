"""HTTP API models (EXECUTION_LOG §3.5, PLAN §5). Frozen contract.

Core types (``JobOptions``, ``Term``, ``Speaker``, ``Segment``, ``ProbeInfo``,
``ConnectionTestResult``) are reused from ``asr``, never duplicated. The frontend TypeScript client
is generated from the OpenAPI document built from these models.
"""

import unicodedata
from datetime import datetime
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from asr.audio.types import ProbeInfo
from asr.correct.llm import LlmProviderName
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
from asr.summarize import TranscriptSummary
from asr.writers import ExportFormat

__all__ = [
    "JOB_STAGES",
    "MAX_UPLOAD_SIZE",
    "CloudSettingsOut",
    "CloudSettingsUpdate",
    "ConfigFileOut",
    "ConnectionTestResult",
    "Device",
    "EditTextOp",
    "ExportFormat",
    "ExportLocale",
    "FsDir",
    "FsDirsOut",
    "FsRoot",
    "HardwareDeviceOut",
    "HardwareOut",
    "HealthOut",
    "JobCreate",
    "JobList",
    "JobOut",
    "JobStatus",
    "LlmSettingsOut",
    "LlmSettingsUpdate",
    "LlmTestOut",
    "LocalModelOut",
    "LocalSettingsOut",
    "LocalSettingsUpdate",
    "SetSpeakerOp",
    "SettingsOut",
    "SettingsUpdate",
    "SiYuanNotebookOut",
    "SiYuanPushIn",
    "SiYuanPushOut",
    "SiYuanSettingsOut",
    "SiYuanSettingsUpdate",
    "SiYuanTestOut",
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
Device = Literal["cpu", "cuda", "rocm"]  # rocm: AMD GPU, runs on torch's HIP-backed cuda device


# -------------------------------------------------------------------------------------- hardware
class HardwareDeviceOut(BaseModel):
    id: Device
    available: bool
    name: str | None = Field(description="CPU model or GPU name; null when unavailable.")
    detail: str | None = Field(
        description="Why the device is unavailable (e.g. 'torch ... is a CPU build'), or facts "
        "about it when available (GPU arch, HIP / CUDA version, core count)."
    )


class HardwareOut(BaseModel):
    """What the installed torch can run on; probed once per server process, no downloads."""

    devices: list[HardwareDeviceOut] = Field(description="Always cpu, cuda, rocm in that order.")
    default: Device = Field(description="The available GPU (cuda or rocm), else cpu.")


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
    summary: TranscriptSummary | None = Field(
        default=None,
        description="LLM summary (M11), null until requested. Stale when `updated_at` is later "
        "than `summary.created_at`.",
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


class SplitOp(BaseModel):
    op: Literal["split"]
    segment_id: str
    at: int = Field(ge=1, description="Character offset in the text where the second part starts.")


class MergeOp(BaseModel):
    op: Literal["merge"]
    segment_id: str = Field(description="Merged with the next segment in time order.")


class ReplaceOp(BaseModel):
    op: Literal["replace"]
    find: str = Field(min_length=1, max_length=200)
    replace: str = Field(max_length=200)
    ignore_case: bool = Field(default=False, description="Match `find` case-insensitively.")


class MergeSpeakersOp(BaseModel):
    op: Literal["merge_speakers"]
    from_id: str = Field(min_length=1, max_length=100)
    into_id: str = Field(min_length=1, max_length=100)


class ReviewCorrectionOp(BaseModel):
    op: Literal["review_correction"]
    correction_id: str = Field(min_length=1, max_length=64)
    decision: Literal["accept", "reject"]


TranscriptOp = Annotated[
    EditTextOp
    | SetSpeakerOp
    | SplitOp
    | MergeOp
    | ReplaceOp
    | MergeSpeakersOp
    | ReviewCorrectionOp,
    Field(discriminator="op"),
]


class LlmSuggestionsIn(BaseModel):
    segment_ids: list[str] | None = Field(
        default=None, max_length=500, description="Segments to check; default all (max 500)."
    )


class LlmSuggestionsOut(BaseModel):
    transcript: TranscriptOut
    added: int = Field(description="New suggested corrections.")
    discarded: int = Field(description="Segment rewrites dropped by the edit-ratio guard.")


_EXPECTED_UPDATED_AT_DOC = (
    "The transcript's `updated_at` as loaded. If it changed since, the request is refused with "
    "409 and nothing is written. Omit to apply on top of the current version."
)


class TranscriptPatch(BaseModel):
    ops: list[TranscriptOp] = Field(min_length=1, max_length=1000)
    expected_updated_at: datetime | None = Field(default=None, description=_EXPECTED_UPDATED_AT_DOC)


# Control characters, line / paragraph separators and bidi overrides: a speaker name is one line
# of plain text in the UI and in every export format.
_BIDI_CONTROLS = frozenset("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


class SpeakerUpdate(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")

    @field_validator("name")
    @classmethod
    def _single_line(cls, v: str) -> str:
        for ch in v:
            if unicodedata.category(ch) in ("Cc", "Zl", "Zp") or ch in _BIDI_CONTROLS:
                raise ValueError("name must not contain control characters or line breaks")
        return v


class SpeakersUpdate(BaseModel):
    speakers: list[SpeakerUpdate] = Field(max_length=100)
    expected_updated_at: datetime | None = Field(default=None, description=_EXPECTED_UPDATED_AT_DOC)


# ------------------------------------------------------------------------------------- term sets
class TermSetIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    terms: list[Term] = Field(default_factory=list, max_length=1000)


class TermSetImport(BaseModel):
    """Plain-text import into a new set.

    One term per line, ``term[<TAB or ,>weight[<TAB or ,>note]]``, where ``term`` may use the
    asr-hotword syntax ``Target | alias … ~~~ black …``.
    """

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


SettingSource = Literal["default", "env", "config", "settings"]


class CloudSettingsOut(BaseModel):
    endpoint: str
    model: str
    file_transfer: FileTransferMode
    api_key_set: bool
    api_key_masked: str | None = Field(description="E.g. 'sk-****abcd'; never the full key.")
    api_key_source: Literal["env", "config", "settings"] | None = Field(
        default=None,
        description="Where the effective DashScope key comes from: `settings` (the override set "
        "via PUT), `config` (the config file), `env` (DASHSCOPE_API_KEY), or null (none). Same "
        "as `sources['cloud.api_key']`, kept for compatibility.",
    )


class LlmSettingsOut(BaseModel):
    """The chat model used for summaries and L3 correction suggestions."""

    provider: LlmProviderName = Field(
        description="`dashscope` (DashScope compatible mode) or `openai` (any OpenAI-compatible "
        "server: llama.cpp, vLLM, Ollama, Lemonade, ...)."
    )
    endpoint: str = Field(
        description="Effective base URL: for dashscope the resolved DashScope endpoint (llm "
        "endpoint, else cloud endpoint); for openai the server base URL incl. /v1, '' when unset."
    )
    model: str
    api_key_set: bool = Field(
        description="A key will be sent. dashscope: the LLM key, else the cloud key. openai: "
        "only an LLM key set at the same or a higher layer than the endpoint."
    )
    api_key_masked: str | None = Field(description="E.g. 'sk-****abcd'; never the full key.")


class ConfigFileOut(BaseModel):
    """The TOML config file (``ASR_CONFIG_FILE`` / ``<data_dir>/config.toml``). No values."""

    path: str | None = Field(description="File in use; null when none is configured or found.")
    loaded: bool = Field(description="The file was read and parsed.")
    error: str | None = Field(
        description="Why the file, or some of its values, was ignored. Never contains values."
    )
    warnings: list[str] = Field(
        description="Unknown keys, loose file permissions, and layered values that do not apply "
        "(e.g. a key that is not sent to an endpoint set at a higher layer)."
    )


class EnvironmentOut(BaseModel):
    """Read-only facts from the server environment (ARCH_REVIEW §4-f). Not updatable via
    ``PUT /api/settings``; changed by editing the env / ``.env`` and restarting. No secrets."""

    api_key_required: bool = Field(description="ASR_API_KEY is set (the key itself never is).")
    allow_unauthenticated_network: bool = Field(description="ASR_ALLOW_UNAUTHENTICATED_NETWORK.")
    cors_origins: list[str] = Field(description="ASR_CORS_ORIGINS.")
    allowed_hosts: list[str] = Field(description="ASR_ALLOWED_HOSTS.")
    upload_roots: list[str] = Field(description="ASR_UPLOAD_ROOTS (resolved).")
    data_dir: str = Field(description="ASR_DATA_DIR (resolved).")
    model_cache_dir: str = Field(description="ASR_MODEL_CACHE_DIR (resolved).")
    decoder: str = Field(description="ASR_DECODER.")
    torch_threads: int | None = Field(description="ASR_TORCH_THREADS; null = automatic.")
    local_dtype: str = Field(description="ASR_LOCAL_DTYPE.")
    llm_model: str = Field(description="ASR_LLM_MODEL (L3 suggestions / summary).")
    log_level: str = Field(description="ASR_LOG_LEVEL.")
    log_format: str = Field(description="ASR_LOG_FORMAT.")
    log_file: str | None = Field(description="ASR_LOG_FILE.")


class SettingsOut(BaseModel):
    provider_default: ProviderName
    api_key_required: bool
    upload_roots: list[str]
    local: LocalSettingsOut
    cloud: CloudSettingsOut
    llm: LlmSettingsOut
    sources: dict[str, SettingSource] = Field(
        description="Layer each online setting comes from (`cloud.endpoint`, `cloud.model`, "
        "`cloud.file_transfer`, `cloud.api_key`, `llm.provider`, `llm.endpoint`, `llm.model`, "
        "`llm.api_key`): settings > config > env > default. For dashscope, `default` for "
        "`llm.endpoint` / `llm.api_key` means the cloud value is inherited."
    )
    config_file: ConfigFileOut
    environment: EnvironmentOut
    env_only: dict[str, str] = Field(
        description="Fields of this response that only the environment sets (dotted path -> "
        "variable name), for a 'set via environment' hint in the UI."
    )


class LocalSettingsUpdate(BaseModel):
    asr_model: str | None = Field(default=None, max_length=200)
    diarize_model: str | None = Field(default=None, max_length=200)
    device: Device | None = None


class CloudSettingsUpdate(BaseModel):
    """Omitted = unchanged; ``""`` = clear the Settings override (config file / env / default
    apply again)."""

    endpoint: str | None = Field(
        default=None, max_length=200, description="Must be an allowed DashScope endpoint (D2)."
    )
    model: str | None = Field(default=None, max_length=200)
    file_transfer: FileTransferMode | Literal[""] | None = None
    api_key: SecretStr | None = Field(
        default=None,
        max_length=500,
        description="Write-only. Non-empty sets the override, '' clears it, omitted leaves it.",
    )


class LlmSettingsUpdate(BaseModel):
    """Omitted = unchanged; ``""`` = clear the Settings override (config file / env / default
    apply again).

    Trust: with provider ``openai`` the transcript text (and the LLM key, if set) is sent to
    whatever base URL is configured here, so only point it at a server you control. Changing the
    effective endpoint or provider to an ``openai`` endpoint without sending ``api_key`` in the
    same request clears the stored LLM key, so a key is never re-used for a new host.
    """

    provider: LlmProviderName | Literal[""] | None = None
    endpoint: str | None = Field(
        default=None,
        max_length=500,
        description="dashscope: an allowed DashScope endpoint ('' = the cloud endpoint). openai: "
        "the server's http(s) base URL incl. /v1, e.g. http://192.168.2.10:8080/v1; no "
        "credentials, query or fragment.",
    )
    model: str | None = Field(default=None, max_length=200)
    api_key: SecretStr | None = Field(
        default=None,
        max_length=500,
        description="Write-only. Non-empty sets the override, '' clears it, omitted leaves it. "
        "dashscope: empty = the cloud key; openai: optional.",
    )


class SettingsUpdate(BaseModel):
    """Partial update: omitted fields are unchanged. Env-only settings are not updatable (D7)."""

    provider_default: ProviderName | None = None
    local: LocalSettingsUpdate | None = None
    cloud: CloudSettingsUpdate | None = None
    llm: LlmSettingsUpdate | None = None


class LlmTestOut(BaseModel):
    ok: bool
    message: str = Field(description="Redacted; never contains the key.")
    model: str | None = Field(
        description="Model the server reports (else the configured one); null when no request "
        "was made."
    )


class LocalModelOut(BaseModel):
    id: str = Field(description="Registry key, as used by `asr models download <id>`.")
    name: str = Field(description="Hub repository id.")
    kind: Literal["asr", "aligner", "vad", "diarization"]
    size_mb: int | None = None
    downloaded: bool
    status: Literal["missing", "downloading", "downloaded", "failed"] = "missing"
    error: str | None = None
    has_mirror: bool = Field(default=False, description="Also available on ModelScope.")


# ---------------------------------------------------------------------------- export / SiYuan
ExportLocale = Literal["zh", "en"]
_SIYUAN_ID = r"^[0-9]{14}-[0-9a-z]{7}$"


class SiYuanSettingsOut(BaseModel):
    endpoint: str = Field(description="SiYuan kernel URL as reached from the backend host.")
    token_set: bool
    token_masked: str | None = Field(description="E.g. '****abcd'; never the full token.")
    notebook: str | None = Field(description="Default notebook id, null if none chosen.")
    path_template: str = Field(
        description="Document path; placeholders {title} {date} {time} {source} {provider}."
    )
    path_fields: list[str] = Field(description="Placeholders allowed in `path_template`.")


class SiYuanSettingsUpdate(BaseModel):
    """Partial update; omitted fields are unchanged and ``""`` resets a field to its default.
    Changing ``endpoint`` without sending ``token`` in the same request clears the stored token,
    so a saved token is never sent to a host it was not saved for."""

    endpoint: str | None = Field(
        default=None, max_length=500, description="http(s) URL; default http://127.0.0.1:6806."
    )
    token: SecretStr | None = Field(
        default=None,
        max_length=500,
        description="Write-only API token (SiYuan: Settings - About). '' clears it.",
    )
    notebook: str | None = Field(default=None, max_length=40, description="Notebook id or ''.")
    path_template: str | None = Field(default=None, max_length=500)


class SiYuanTestOut(BaseModel):
    ok: bool
    message: str
    version: str | None = None
    notebooks: int | None = Field(default=None, description="Number of notebooks visible.")


class SiYuanNotebookOut(BaseModel):
    id: str
    name: str
    closed: bool = Field(description="Closed notebooks cannot receive documents.")


class SiYuanPushIn(BaseModel):
    notebook: str | None = Field(
        default=None, pattern=_SIYUAN_ID, description="Notebook id; default from settings."
    )
    path: str | None = Field(
        default=None,
        max_length=1000,
        description="Document path such as '/会议记录/周会'; default from `path_template`. "
        "An existing path gets a ' (2)' suffix.",
    )
    locale: ExportLocale = "zh"
    timestamps: bool = True
    merge_same_speaker: bool = False


class SiYuanPushOut(BaseModel):
    doc_id: str
    notebook: str
    path: str = Field(description="Path actually used (may carry a ' (n)' suffix).")
    url: str = Field(description="siyuan://blocks/<doc_id>, opens the document in SiYuan.")
    warnings: list[str] = Field(default_factory=list)
