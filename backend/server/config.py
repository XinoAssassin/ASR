"""Environment configuration (``.env.example``).

``EnvSettings`` holds values that come only from the process environment / ``.env`` files.
Runtime-overridable settings (DashScope key, endpoint, model, default provider; EXECUTION_LOG D7)
are layered on top of these from the ``settings`` table by ``backend-jobs``.

Tests: build an app with ``create_app(env)`` and an explicit ``EnvSettings``; or set env vars,
``monkeypatch.setattr(config, "ENV_FILES", ())`` and call ``get_env.cache_clear()``
(``tests/conftest.py`` does both).
"""

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from asr.providers.base import FileTransferMode, ProviderName

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
    upload_roots: Annotated[list[Path], NoDecode] = Field(
        default_factory=list,
        validation_alias="ASR_UPLOAD_ROOTS",
        description="os.pathsep-separated allowed upload roots; default <data_dir>/uploads.",
    )
    data_dir: Path = Field(default=Path("./data"), validation_alias="ASR_DATA_DIR")
    model_cache_dir: Path = Field(default=Path("./models"), validation_alias="ASR_MODEL_CACHE_DIR")
    device: Literal["cpu", "cuda"] = Field(default="cpu", validation_alias="ASR_DEVICE")
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
        object.__setattr__(self, "data_dir", data_dir)
        object.__setattr__(self, "upload_roots", resolved)
        object.__setattr__(self, "model_cache_dir", self.model_cache_dir.expanduser().resolve())
        return self

    @property
    def api_key_required(self) -> bool:
        return bool(self.api_key.get_secret_value())

    @property
    def db_path(self) -> Path:
        return self.data_dir / "asr.db"


def load_env(env_files: tuple[Path, ...] | None = None) -> EnvSettings:
    """Read settings from the environment and ``env_files`` (default ``ENV_FILES``)."""
    files = ENV_FILES if env_files is None else env_files
    return EnvSettings(_env_file=files or None)  # type: ignore[call-arg]


@lru_cache(maxsize=1)
def get_env() -> EnvSettings:
    """Process-wide cached settings. Call ``get_env.cache_clear()`` after changing the env."""
    return load_env()


def ensure_dirs(env: EnvSettings) -> None:
    """Create the data dir and every upload root (startup)."""
    env.data_dir.mkdir(parents=True, exist_ok=True)
    for root in env.upload_roots:
        root.mkdir(parents=True, exist_ok=True)
