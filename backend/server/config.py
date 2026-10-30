"""Environment configuration (``.env.example``).

``EnvSettings`` holds values that come only from the process environment / ``.env`` files.
Runtime-overridable settings (default provider and local model choices; EXECUTION_LOG D7) are
layered on top of these from the ``settings`` table. The online settings (``cloud.*``: DashScope
endpoint, key, model, file transfer; ``llm.*``: provider, endpoint, model, key for summaries and
L3 suggestions) have four layers, highest first: settings table (UI) > config file
(``server.config_file``) > environment > built-in default. See ``effective_settings(env,
overrides)`` / ``load_effective_settings(env, db)``. ``ASR_API_KEY``, upload roots and CORS
origins stay env-only.

Tests: build an app with ``create_app(env)`` and an explicit ``EnvSettings``; or set env vars,
``monkeypatch.setattr(config, "ENV_FILES", ())`` and call ``get_env.cache_clear()``
(``tests/conftest.py`` does both).
"""

import asyncio
import logging
import os
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from asr.correct.llm import LlmProviderName, normalize_base_url
from asr.providers.base import FileTransferMode, ProviderName
from server import config_file as cfgfile

if TYPE_CHECKING:
    from server.db import Database

log = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent

# Later files win; real environment variables beat both.
ENV_FILES: tuple[Path, ...] = (REPO_ROOT / ".env", BACKEND_DIR / ".env")

DEFAULT_CLOUD_ENDPOINT = "https://dashscope.aliyuncs.com"
DEFAULT_CLOUD_MODEL = "qwen-audio-3.1-asr-flash-filetrans"


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(
        extra="ignore",
        populate_by_name=True,
        env_file_encoding="utf-8",
        frozen=True,
    )

    api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias="ASR_API_KEY",
        description="Shared X-API-Key; empty disables auth.",
    )
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://localhost:8080"],
        validation_alias="ASR_CORS_ORIGINS",
        description="Comma-separated allow-list of browser origins. '*' is rejected.",
    )
    allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        validation_alias="ASR_ALLOWED_HOSTS",
        description="Comma-separated extra Host names accepted while auth is disabled "
        "(loopback names are always accepted; '*' accepts any host).",
    )
    allow_unauthenticated_network: bool = Field(
        default=False,
        validation_alias="ASR_ALLOW_UNAUTHENTICATED_NETWORK",
        description="With an empty ASR_API_KEY, accept non-loopback peers and let `asr serve` "
        "bind a non-loopback address. Only safe when the port is published on loopback only "
        "(docker-compose's 127.0.0.1:8000:8000).",
    )
    upload_roots: Annotated[list[Path], NoDecode] = Field(
        default_factory=list,
        validation_alias="ASR_UPLOAD_ROOTS",
        description="os.pathsep-separated allowed upload roots; default <data_dir>/uploads.",
    )
    data_dir: Path = Field(default=Path("./data"), validation_alias="ASR_DATA_DIR")
    model_cache_dir: Path = Field(default=Path("./models"), validation_alias="ASR_MODEL_CACHE_DIR")
    device: Literal["cpu", "cuda", "rocm"] = Field(default="cpu", validation_alias="ASR_DEVICE")
    decoder: Literal["pyav", "imageio"] = Field(default="pyav", validation_alias="ASR_DECODER")
    provider_default: ProviderName = Field(default="cloud", validation_alias="ASR_PROVIDER_DEFAULT")

    dashscope_api_key: SecretStr = Field(
        default=SecretStr(""), validation_alias="DASHSCOPE_API_KEY"
    )
    dashscope_endpoint: str = Field(
        default=DEFAULT_CLOUD_ENDPOINT, validation_alias="DASHSCOPE_ENDPOINT"
    )
    cloud_model: str = Field(default=DEFAULT_CLOUD_MODEL, validation_alias="ASR_CLOUD_MODEL")
    cloud_file_transfer: FileTransferMode = Field(
        default="temp_upload", validation_alias="ASR_CLOUD_FILE_TRANSFER"
    )
    llm_model: str = Field(
        default="qwen-plus",
        validation_alias="ASR_LLM_MODEL",
        description="Chat model for summaries and [5b] L3 correction suggestions.",
    )
    llm_provider: Literal["", "dashscope", "openai"] = Field(
        default="",
        validation_alias="ASR_LLM_PROVIDER",
        description="dashscope | openai; empty = dashscope.",
    )
    llm_endpoint: str = Field(
        default="",
        validation_alias="ASR_LLM_ENDPOINT",
        description="LLM base URL (openai: incl. /v1); empty = the DashScope endpoint.",
    )
    llm_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias="ASR_LLM_API_KEY",
        description="LLM key; empty = the DashScope key (dashscope) or none (openai).",
    )
    config_file: Path | None = Field(
        default=None,
        validation_alias="ASR_CONFIG_FILE",
        description="TOML config file for the online settings; default <data_dir>/config.toml.",
    )

    torch_threads: int | None = Field(
        default=None,
        ge=1,
        le=1024,
        validation_alias="ASR_TORCH_THREADS",
        description="CPU inference threads for the local models; default min(16, CPU count).",
    )
    local_dtype: Literal["auto", "float32", "bfloat16", "float16"] = Field(
        default="auto",
        validation_alias="ASR_LOCAL_DTYPE",
        description="Local model dtype; auto = bfloat16 where native, else float32.",
    )

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO", validation_alias="ASR_LOG_LEVEL"
    )
    log_format: Literal["text", "json"] = Field(default="text", validation_alias="ASR_LOG_FORMAT")
    log_file: Path | None = Field(
        default=None,
        validation_alias="ASR_LOG_FILE",
        description="Optional log file, rotated at 10 MB (5 backups).",
    )

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_level(cls, v: Any) -> Any:
        return v.strip().upper() if isinstance(v, str) else v

    @field_validator("torch_threads", mode="before")
    @classmethod
    def _empty_threads(cls, v: Any) -> Any:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("llm_provider", mode="before")
    @classmethod
    def _lower_provider(cls, v: Any) -> Any:
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("local_dtype", mode="before")
    @classmethod
    def _lower_dtype(cls, v: Any) -> Any:
        return (v.strip().lower() or "auto") if isinstance(v, str) else v

    @field_validator("allow_unauthenticated_network", mode="before")
    @classmethod
    def _empty_flag(cls, v: Any) -> Any:
        return False if isinstance(v, str) and not v.strip() else v

    @field_validator("log_file", "config_file", mode="before")
    @classmethod
    def _empty_path(cls, v: Any) -> Any:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.split(",")
        if isinstance(v, list | tuple):
            origins = [str(o).strip().rstrip("/") for o in v if str(o).strip()]
            if "*" in origins:
                raise ValueError("ASR_CORS_ORIGINS must list explicit origins, not '*'")
            return origins
        return v

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _split_hosts(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [h.strip().lower() for h in v.split(",") if h.strip()]
        return v

    @field_validator("upload_roots", mode="before")
    @classmethod
    def _split_roots(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [p.strip() for p in v.split(os.pathsep) if p.strip()]
        return v

    @field_validator("dashscope_endpoint")
    @classmethod
    def _strip_endpoint(cls, v: str) -> str:
        return v.strip().rstrip("/")

    @model_validator(mode="after")
    def _resolve_paths(self) -> Self:
        # Absolute realpaths, so containment checks compare like with like. Relative values are
        # relative to the process working directory.
        data_dir = self.data_dir.expanduser().resolve()
        roots = self.upload_roots or [data_dir / "uploads"]
        resolved: list[Path] = []
        for root in roots:
            r = Path(root).expanduser().resolve()
            if r not in resolved:
                resolved.append(r)
        for r in resolved:
            # A root at or above the data dir would expose asr.db, caches and upload sessions to
            # uploads, register and the directory picker. Roots below the data dir are fine.
            if data_dir == r or r in data_dir.parents:
                raise ValueError(
                    f"upload root {r} contains ASR_DATA_DIR {data_dir}; use a separate directory"
                )
        object.__setattr__(self, "data_dir", data_dir)
        object.__setattr__(self, "upload_roots", resolved)
        object.__setattr__(self, "model_cache_dir", self.model_cache_dir.expanduser().resolve())
        if self.config_file is not None:
            object.__setattr__(self, "config_file", self.config_file.expanduser().resolve())
        return self

    @property
    def api_key_required(self) -> bool:
        return bool(self.api_key.get_secret_value())

    @property
    def db_path(self) -> Path:
        return self.data_dir / "asr.db"


class ConfigError(ValueError):
    """Invalid environment configuration; the message never contains submitted values."""


def load_env(env_files: tuple[Path, ...] | None = None) -> EnvSettings:
    """Read settings from the environment and ``env_files`` (default ``ENV_FILES``).

    Pydantic's ``ValidationError`` text includes the offending input, which for model-level
    validators is the whole settings dict with ``ASR_API_KEY`` / ``DASHSCOPE_API_KEY``. Re-raise
    with location and message only, and without chaining, so secrets never reach stderr.
    """
    files = ENV_FILES if env_files is None else env_files
    try:
        return EnvSettings(_env_file=files or None)  # type: ignore[call-arg]
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'settings'}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)
        )
        raise ConfigError(f"invalid configuration: {problems}") from None


@lru_cache(maxsize=1)
def get_env() -> EnvSettings:
    """Process-wide cached settings. Call ``get_env.cache_clear()`` after changing the env."""
    return load_env()


def core_environ(env: EnvSettings) -> dict[str, str | None]:
    """Variables the Web-free core library reads from ``os.environ`` (None = unset)."""
    return {
        "ASR_DECODER": env.decoder,
        "ASR_TORCH_THREADS": None if env.torch_threads is None else str(env.torch_threads),
        "ASR_LOCAL_DTYPE": env.local_dtype,
    }


def apply_core_environ(env: EnvSettings) -> None:
    """Mirror ``core_environ`` into the process env, so ``.env`` values reach the core library."""
    for name, value in core_environ(env).items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def load_env_files_into_environ(env_files: tuple[Path, ...] | None = None) -> list[str]:
    """Copy variables from the ``.env`` files (default ``ENV_FILES``; later files win) into
    ``os.environ`` for CLI commands. Real environment variables win. Returns the names set
    (never the values)."""
    from dotenv import dotenv_values

    merged: dict[str, str] = {}
    for path in ENV_FILES if env_files is None else env_files:
        if path.is_file():
            merged.update(
                {k: v for k, v in dotenv_values(path, encoding="utf-8").items() if v is not None}
            )
    added = [name for name in merged if name not in os.environ]
    for name in added:
        os.environ[name] = merged[name]
    return added


def ensure_dirs(env: EnvSettings) -> None:
    """Create the data dir and every upload root (startup)."""
    env.data_dir.mkdir(parents=True, exist_ok=True)
    for root in env.upload_roots:
        root.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------------------- settings overlay
# Keys of the ``settings`` table that may override lower layers (D7).
SETTING_PROVIDER_DEFAULT = "provider_default"
SETTING_CLOUD_ENDPOINT = "cloud.endpoint"
SETTING_CLOUD_MODEL = "cloud.model"
SETTING_CLOUD_API_KEY = "cloud.api_key"
SETTING_CLOUD_FILE_TRANSFER = "cloud.file_transfer"
SETTING_LLM_PROVIDER = "llm.provider"
SETTING_LLM_ENDPOINT = "llm.endpoint"
SETTING_LLM_MODEL = "llm.model"
SETTING_LLM_API_KEY = "llm.api_key"
SETTING_LOCAL_ASR_MODEL = "local.asr_model"
SETTING_LOCAL_DIARIZE_MODEL = "local.diarize_model"
SETTING_LOCAL_DEVICE = "local.device"

# The online settings: settings table > config file > environment > default.
ONLINE_SETTINGS: tuple[str, ...] = (
    SETTING_CLOUD_ENDPOINT,
    SETTING_CLOUD_MODEL,
    SETTING_CLOUD_FILE_TRANSFER,
    SETTING_CLOUD_API_KEY,
    SETTING_LLM_PROVIDER,
    SETTING_LLM_ENDPOINT,
    SETTING_LLM_MODEL,
    SETTING_LLM_API_KEY,
)

OVERRIDABLE_SETTINGS: frozenset[str] = frozenset(
    {
        SETTING_PROVIDER_DEFAULT,
        SETTING_LOCAL_ASR_MODEL,
        SETTING_LOCAL_DIARIZE_MODEL,
        SETTING_LOCAL_DEVICE,
        *ONLINE_SETTINGS,
    }
)

Source = Literal["default", "env", "config", "settings"]
_RANK: dict[str, int] = {"default": 0, "env": 1, "config": 2, "settings": 3}

# Settings key -> the EnvSettings field of its env layer.
_ENV_FIELDS: dict[str, str] = {
    SETTING_CLOUD_ENDPOINT: "dashscope_endpoint",
    SETTING_CLOUD_MODEL: "cloud_model",
    SETTING_CLOUD_FILE_TRANSFER: "cloud_file_transfer",
    SETTING_CLOUD_API_KEY: "dashscope_api_key",
    SETTING_LLM_PROVIDER: "llm_provider",
    SETTING_LLM_ENDPOINT: "llm_endpoint",
    SETTING_LLM_MODEL: "llm_model",
    SETTING_LLM_API_KEY: "llm_api_key",
}


class ConfigFileStatus(BaseModel):
    """The config file as ``GET /api/settings`` reports it: no values."""

    model_config = ConfigDict(frozen=True)

    path: str | None = Field(description="File in use; null when none is configured or found.")
    loaded: bool = Field(description="The file was read and parsed.")
    error: str | None = Field(
        description="Why the file (or some of its values) was ignored; never contains values."
    )
    warnings: list[str] = Field(
        description="Unknown keys, loose file permissions, values not applicable as layered."
    )


class EffectiveSettings(BaseModel):
    """All layers merged. Keys are ``SecretStr``.

    ``llm_endpoint`` is the effective base URL (for ``dashscope`` the resolved DashScope
    endpoint, else the OpenAI-compatible base URL, ``""`` when not configured); ``llm_api_key``
    is the key that will be sent (for ``dashscope`` the LLM key or else the cloud key; for
    ``openai`` only an LLM key set at the same or a higher layer than the endpoint, so a key
    from the config file or env is never sent to an endpoint someone typed into Settings).
    """

    model_config = ConfigDict(frozen=True)

    provider_default: ProviderName
    cloud_endpoint: str
    cloud_model: str
    cloud_file_transfer: FileTransferMode
    cloud_api_key: SecretStr
    llm_provider: LlmProviderName = "dashscope"
    llm_endpoint: str = DEFAULT_CLOUD_ENDPOINT
    llm_model: str = "qwen-plus"
    llm_api_key: SecretStr = SecretStr("")
    local_asr_model: str | None
    local_diarize_model: str | None
    local_device: Literal["cpu", "cuda", "rocm"]
    model_cache_dir: Path
    sources: dict[str, Source] = Field(default_factory=dict)
    config_file: ConfigFileStatus = ConfigFileStatus(
        path=None, loaded=False, error=None, warnings=[]
    )

    @property
    def cloud_api_key_set(self) -> bool:
        return bool(self.cloud_api_key.get_secret_value())

    @property
    def llm_api_key_set(self) -> bool:
        return bool(self.llm_api_key.get_secret_value())

    def llm_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for ``LlmCorrector`` / ``Summarizer`` / ``check_llm``."""
        return {
            "provider": self.llm_provider,
            "endpoint": self.llm_endpoint,
            "model": self.llm_model,
            "api_key": self.llm_api_key.get_secret_value(),
        }


def _env_value(env: EnvSettings, key: str) -> str | None:
    """The environment's value for ``key``; ``None`` when not set (or empty)."""
    name = _ENV_FIELDS[key]
    if name not in env.model_fields_set:
        return None
    value = getattr(env, name)
    text = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
    return text.strip() or None


def _layers(
    env: EnvSettings, overrides: Mapping[str, str], file: Mapping[str, str], key: str
) -> list[tuple[Source, str]]:
    """Non-empty values for ``key``, highest precedence first."""
    out: list[tuple[Source, str]] = []
    for source, raw in (
        ("settings", overrides.get(key)),
        ("config", file.get(key)),
        ("env", _env_value(env, key)),
    ):
        if raw is not None and raw.strip():
            out.append((source, raw))  # type: ignore[arg-type]
    return out


def _pick(
    layers: list[tuple[Source, str]],
    validate: Any,
    key: str,
    notes: list[str],
) -> tuple[str, Source] | None:
    """First layer whose value validates. An invalid stored / env value is skipped with a
    warning naming the key and layer only (config-file values were validated on load)."""
    for source, raw in layers:
        try:
            return validate(raw), source
        except ValueError:
            if source != "config":
                log.warning("ignoring invalid %s value for %r", source, key)
            notes.append(f"{key} from {_SOURCE_NAME[source]} is invalid and was ignored")
    return None


_SOURCE_NAME = {"settings": "Settings", "config": "the config file", "env": "the environment"}


def _lenient_endpoint(value: str) -> str:
    """Env / stored cloud endpoints are used as given (the provider checks the allow-list and
    the connection test reports it), as before the config layer existed."""
    return value.strip().rstrip("/")


def _online(
    env: EnvSettings, overrides: Mapping[str, str], file: Mapping[str, str]
) -> tuple[dict[str, Any], dict[str, Source], list[str]]:
    values: dict[str, Any] = {}
    sources: dict[str, Source] = {}
    notes: list[str] = []

    def resolve(key: str, validate: Any, default: str) -> tuple[str, Source]:
        picked = _pick(_layers(env, overrides, file, key), validate, key, notes)
        return picked if picked is not None else (default, "default")

    values["cloud_endpoint"], sources[SETTING_CLOUD_ENDPOINT] = resolve(
        SETTING_CLOUD_ENDPOINT, _lenient_endpoint, DEFAULT_CLOUD_ENDPOINT
    )
    values["cloud_model"], sources[SETTING_CLOUD_MODEL] = resolve(
        SETTING_CLOUD_MODEL, cfgfile.valid_model, DEFAULT_CLOUD_MODEL
    )
    values["cloud_file_transfer"], sources[SETTING_CLOUD_FILE_TRANSFER] = resolve(
        SETTING_CLOUD_FILE_TRANSFER, cfgfile.valid_file_transfer, "temp_upload"
    )
    cloud_key, sources[SETTING_CLOUD_API_KEY] = resolve(
        SETTING_CLOUD_API_KEY, cfgfile.valid_api_key, ""
    )
    values["cloud_api_key"] = SecretStr(cloud_key)

    provider, sources[SETTING_LLM_PROVIDER] = resolve(
        SETTING_LLM_PROVIDER, cfgfile.valid_llm_provider, "dashscope"
    )
    values["llm_provider"] = provider
    values["llm_model"], sources[SETTING_LLM_MODEL] = resolve(
        SETTING_LLM_MODEL, cfgfile.valid_model, "qwen-plus"
    )

    # The endpoint must suit the effective provider, whichever layer that came from.
    def endpoint_for_provider(value: str) -> str:
        if provider == "dashscope":
            return cfgfile.valid_cloud_endpoint(value)
        return normalize_base_url(value)

    endpoint_layers = _layers(env, overrides, file, SETTING_LLM_ENDPOINT)
    picked = None
    for source, raw in endpoint_layers:
        try:
            picked = (endpoint_for_provider(raw), source)
            break
        except ValueError:
            notes.append(
                f"llm.endpoint from {_SOURCE_NAME[source]} does not suit provider "
                f"{provider} and was ignored"
            )
    if picked is not None:
        values["llm_endpoint"], sources[SETTING_LLM_ENDPOINT] = picked
    else:
        # dashscope: inherit the cloud endpoint; openai: not configured.
        values["llm_endpoint"] = values["cloud_endpoint"] if provider == "dashscope" else ""
        sources[SETTING_LLM_ENDPOINT] = "default"

    key_layers = _layers(env, overrides, file, SETTING_LLM_API_KEY)
    key_pick = _pick(key_layers, cfgfile.valid_api_key, SETTING_LLM_API_KEY, notes)
    if provider == "dashscope":
        if key_pick is not None:
            values["llm_api_key"] = SecretStr(key_pick[0])
            sources[SETTING_LLM_API_KEY] = key_pick[1]
        else:  # inherit the DashScope key
            values["llm_api_key"] = values["cloud_api_key"]
            sources[SETTING_LLM_API_KEY] = "default"
    else:
        endpoint_rank = _RANK[sources[SETTING_LLM_ENDPOINT]]
        if key_pick is not None and _RANK[key_pick[1]] >= endpoint_rank:
            values["llm_api_key"] = SecretStr(key_pick[0])
            sources[SETTING_LLM_API_KEY] = key_pick[1]
        else:
            values["llm_api_key"] = SecretStr("")
            sources[SETTING_LLM_API_KEY] = "default"
            if key_pick is not None:
                notes.append(
                    f"llm.api_key from {_SOURCE_NAME[key_pick[1]]} is not sent to the "
                    f"llm.endpoint set in {_SOURCE_NAME[sources[SETTING_LLM_ENDPOINT]]}; "
                    "set the key there too"
                )
    return values, sources, notes


def effective_settings(
    env: EnvSettings,
    overrides: Mapping[str, str],
    file: cfgfile.ConfigFileState | None = None,
) -> EffectiveSettings:
    """Overlay ``overrides`` (settings-table rows) and the config file (default: read it for
    ``env``) on ``env``.

    An override that no longer validates (e.g. an old provider name) is ignored with a warning
    that names the key only, never the value.
    """
    if file is None:
        file = cfgfile.load(env)
    values: dict[str, Any] = {
        "provider_default": env.provider_default,
        "local_asr_model": None,
        "local_diarize_model": None,
        "local_device": env.device,
        "model_cache_dir": env.model_cache_dir,
    }
    online, sources, notes = _online(env, overrides, file.values)
    values.update(online)
    mapping = {
        SETTING_PROVIDER_DEFAULT: "provider_default",
        SETTING_LOCAL_ASR_MODEL: "local_asr_model",
        SETTING_LOCAL_DIARIZE_MODEL: "local_diarize_model",
        SETTING_LOCAL_DEVICE: "local_device",
    }
    for key, field in mapping.items():
        raw = overrides.get(key)
        if raw is None or raw == "":
            continue
        try:
            candidate = EffectiveSettings(**{**values, field: raw})
        except ValidationError:
            log.warning("ignoring invalid stored setting %r", key)
            continue
        values[field] = getattr(candidate, field)
    info = file.info
    values["sources"] = sources
    values["config_file"] = ConfigFileStatus(
        path=info.path,
        loaded=info.loaded,
        error=info.error,
        warnings=[*info.warnings, *dict.fromkeys(notes)],
    )
    return EffectiveSettings(**values)


async def load_effective_settings(env: EnvSettings, db: "Database") -> EffectiveSettings:
    overrides = await db.get_settings()
    file = await asyncio.to_thread(cfgfile.load, env)
    return effective_settings(env, overrides, file)


def mask_secret(secret: SecretStr | str | None) -> str | None:
    """``"sk-test-1234567890abcd" -> "sk-****abcd"``; ``None`` when unset.

    Only the last four characters are revealed, and only for keys long enough that four
    characters are a small fraction of the key.
    """
    if secret is None:
        return None
    value = secret.get_secret_value() if isinstance(secret, SecretStr) else secret
    if not value:
        return None
    if len(value) < 12:
        return "****"
    prefix = "sk-" if value.startswith("sk-") else ""
    return f"{prefix}****{value[-4:]}"


# ------------------------------------------------------------------------------ SiYuan export
# Settings-table keys for the SiYuan push (docs/research/siyuan-export.md). Table-only: there is
# no env default, so the token is always bound to the endpoint it was saved with (changing the
# endpoint without a new token clears it, see routes/siyuan.py).
SETTING_SIYUAN_ENDPOINT = "siyuan.endpoint"
SETTING_SIYUAN_TOKEN = "siyuan.token"  # noqa: S105 - a settings key, not a secret
SETTING_SIYUAN_NOTEBOOK = "siyuan.notebook"
SETTING_SIYUAN_PATH_TEMPLATE = "siyuan.path_template"

SIYUAN_SETTINGS: frozenset[str] = frozenset(
    {
        SETTING_SIYUAN_ENDPOINT,
        SETTING_SIYUAN_TOKEN,
        SETTING_SIYUAN_NOTEBOOK,
        SETTING_SIYUAN_PATH_TEMPLATE,
    }
)


class SiYuanSettings(BaseModel):
    """Effective SiYuan settings. The token is a ``SecretStr`` (never returned or logged)."""

    model_config = ConfigDict(frozen=True)

    endpoint: str
    token: SecretStr
    notebook: str | None
    path_template: str

    @property
    def token_set(self) -> bool:
        return bool(self.token.get_secret_value())


def siyuan_settings(overrides: Mapping[str, str]) -> SiYuanSettings:
    """Defaults overlaid with the stored values; a stored value that no longer validates is
    ignored with a warning naming the key only."""
    from asr.export import siyuan

    endpoint = siyuan.DEFAULT_ENDPOINT
    template = siyuan.DEFAULT_PATH_TEMPLATE
    notebook: str | None = None
    if raw := overrides.get(SETTING_SIYUAN_ENDPOINT):
        try:
            endpoint = siyuan.normalize_endpoint(raw)
        except ValueError:
            log.warning("ignoring invalid stored setting %r", SETTING_SIYUAN_ENDPOINT)
    if raw := overrides.get(SETTING_SIYUAN_PATH_TEMPLATE):
        try:
            template = siyuan.validate_path_template(raw)
        except ValueError:
            log.warning("ignoring invalid stored setting %r", SETTING_SIYUAN_PATH_TEMPLATE)
    if raw := overrides.get(SETTING_SIYUAN_NOTEBOOK):
        if siyuan.is_block_id(raw):
            notebook = raw
        else:
            log.warning("ignoring invalid stored setting %r", SETTING_SIYUAN_NOTEBOOK)
    return SiYuanSettings(
        endpoint=endpoint,
        token=SecretStr(overrides.get(SETTING_SIYUAN_TOKEN) or ""),
        notebook=notebook,
        path_template=template,
    )


async def load_siyuan_settings(db: "Database") -> SiYuanSettings:
    return siyuan_settings(await db.get_settings())
