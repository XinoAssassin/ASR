"""Optional TOML config file for the online (cloud / LLM) settings (``deploy/config.example.toml``).

Path: ``ASR_CONFIG_FILE``, else ``<ASR_DATA_DIR>/config.toml`` when that exists. A missing default
file is fine; a missing explicit file, a malformed file, unknown keys or invalid values never stop
the server: the bad parts are ignored and reported in ``ConfigFileState.error`` / ``warnings``
(shown by ``GET /api/settings``). Messages name keys and positions only, never values, because
the file holds API keys (AGENTS rule 3).

The file is re-read when its mtime / size / inode / ctime change (one ``stat`` per settings load).

Precedence per field (``server.config.effective_settings``): settings table (UI) > this file >
environment > built-in default.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import threading
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, get_args

from asr.correct.llm import LLM_PROVIDERS, normalize_base_url
from asr.providers.base import FileTransferMode
from asr.providers.cloud.security import redact

if TYPE_CHECKING:
    from server.config import EnvSettings

log = logging.getLogger(__name__)

DEFAULT_NAME = "config.toml"
MAX_BYTES = 64 * 1024
MAX_MODEL_CHARS = 200
MAX_KEY_CHARS = 500

FILE_TRANSFER_MODES: tuple[str, ...] = get_args(FileTransferMode)


# ----------------------------------------------------------------------------------- validators
# Each returns the normalized value or raises ValueError with a message that never contains the
# value. Shared with the settings route and the settings-table layer.
def valid_cloud_endpoint(value: str) -> str:
    from asr.providers import cloud as cloud_provider

    endpoint = value.strip().rstrip("/")
    if not cloud_provider.is_allowed_endpoint(endpoint):
        raise ValueError("is not an allowed DashScope endpoint")
    return endpoint


def valid_model(value: str) -> str:
    model = value.strip()
    if not 1 <= len(model) <= MAX_MODEL_CHARS:
        raise ValueError(f"must be 1-{MAX_MODEL_CHARS} characters")
    if not model.isprintable():
        raise ValueError("must not contain control characters")
    return model


def valid_file_transfer(value: str) -> str:
    mode = value.strip()
    if mode not in FILE_TRANSFER_MODES:
        raise ValueError("must be one of " + ", ".join(FILE_TRANSFER_MODES))
    return mode


def valid_api_key(value: str) -> str:
    key = value.strip()
    if not key or len(key) > MAX_KEY_CHARS:
        raise ValueError(f"must be 1-{MAX_KEY_CHARS} characters")
    if any(c.isspace() for c in key) or not key.isprintable():
        raise ValueError("must not contain whitespace")
    return key


def valid_llm_provider(value: str) -> str:
    provider = value.strip().lower()
    if provider not in LLM_PROVIDERS:
        raise ValueError("must be one of " + ", ".join(LLM_PROVIDERS))
    return provider


def valid_base_url(value: str) -> str:
    return normalize_base_url(value)


# dotted key -> validator. The LLM endpoint is checked against the effective provider later
# (DashScope allow-list or any http(s) base URL); here only its shape.
VALIDATORS: Mapping[str, Callable[[str], str]] = {
    "cloud.endpoint": valid_cloud_endpoint,
    "cloud.api_key": valid_api_key,
    "cloud.model": valid_model,
    "cloud.file_transfer": valid_file_transfer,
    "llm.provider": valid_llm_provider,
    "llm.endpoint": valid_base_url,
    "llm.model": valid_model,
    "llm.api_key": valid_api_key,
}
SECTIONS: frozenset[str] = frozenset(k.split(".", 1)[0] for k in VALIDATORS)


# --------------------------------------------------------------------------------------- state
@dataclass(frozen=True)
class ConfigFileInfo:
    """What ``GET /api/settings`` reports about the file (no values)."""

    path: str | None = None
    loaded: bool = False
    error: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConfigFileState:
    info: ConfigFileInfo = field(default_factory=ConfigFileInfo)
    # Validated, non-empty values by dotted key. Holds API keys: never in repr.
    values: Mapping[str, str] = field(default_factory=dict, repr=False)


EMPTY = ConfigFileState()

_POSITION = re.compile(r"\(at line (\d+), column (\d+)\)")


def config_path(env: EnvSettings) -> tuple[Path | None, bool]:
    """``(path, explicit)``: ``ASR_CONFIG_FILE`` or the default ``<data_dir>/config.toml``."""
    if env.config_file is not None:
        return env.config_file, True
    return env.data_dir / DEFAULT_NAME, False


def parse(raw: bytes, path: str) -> ConfigFileState:
    """Validate a config file's bytes. Never raises; never puts a value in a message."""
    if len(raw) > MAX_BYTES:
        return ConfigFileState(
            ConfigFileInfo(path, False, f"config file is larger than {MAX_BYTES // 1024} KiB")
        )
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return ConfigFileState(ConfigFileInfo(path, False, "config file is not valid UTF-8"))
    except tomllib.TOMLDecodeError as exc:
        # The decoder's message describes the syntax problem, not the value; keep only the
        # position anyway, so a mangled key line can never be echoed.
        m = _POSITION.search(str(exc))
        where = f" (line {m.group(1)}, column {m.group(2)})" if m else ""
        return ConfigFileState(ConfigFileInfo(path, False, f"config file is not valid TOML{where}"))
    errors: list[str] = []
    warnings: list[str] = []
    values: dict[str, str] = {}
    for section, table in data.items():
        if section not in SECTIONS:
            warnings.append(redact(f"unknown section [{section[:50]}] ignored"))
            continue
        if not isinstance(table, dict):
            errors.append(f"{section} must be a table ([{section}])")
            continue
        for name, value in table.items():
            key = f"{section}.{name}"
            validator = VALIDATORS.get(key)
            if validator is None:
                warnings.append(redact(f"unknown key {section}.{name[:50]} ignored"))
                continue
            if not isinstance(value, str):
                errors.append(f"{key} must be a string (ignored)")
                continue
            if not value.strip():
                continue  # empty = not set in this layer
            try:
                values[key] = validator(value)
            except ValueError as exc:
                errors.append(f"{key} {exc} (ignored)")
    return ConfigFileState(
        ConfigFileInfo(path, True, "; ".join(errors) or None, tuple(warnings)), values
    )


def _permission_warning(st: os.stat_result) -> str | None:
    if os.name != "posix":
        return None
    if st.st_mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
        return (
            "config file is readable or writable by other users; it may hold API keys (chmod 600)"
        )
    return None


_cache: dict[Path, tuple[tuple[int, ...], ConfigFileState]] = {}
_lock = threading.Lock()


def load(env: EnvSettings) -> ConfigFileState:
    """The current config file state (cached until the file changes). Never raises."""
    path, explicit = config_path(env)
    if path is None:
        return EMPTY
    shown = str(path)
    try:
        st = path.stat()
    except FileNotFoundError:
        if not explicit:
            return EMPTY
        return ConfigFileState(ConfigFileInfo(shown, False, "config file not found"))
    except OSError as exc:
        return ConfigFileState(
            ConfigFileInfo(shown, False, f"config file cannot be read ({type(exc).__name__})")
        )
    if not stat.S_ISREG(st.st_mode):
        return ConfigFileState(ConfigFileInfo(shown, False, "config file is not a regular file"))
    stamp = (st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino, st.st_mode)
    with _lock:
        cached = _cache.get(path)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        try:
            with path.open("rb") as fh:
                raw = fh.read(MAX_BYTES + 1)
        except OSError as exc:
            state = ConfigFileState(
                ConfigFileInfo(shown, False, f"config file cannot be read ({type(exc).__name__})")
            )
        else:
            state = parse(raw, shown)
            if warning := _permission_warning(st):
                info = state.info
                state = ConfigFileState(
                    ConfigFileInfo(info.path, info.loaded, info.error, (*info.warnings, warning)),
                    state.values,
                )
        _cache[path] = (stamp, state)
        _log(state)
    return state


def _log(state: ConfigFileState) -> None:
    """Once per change of the file: what was loaded (key names only) and any problems."""
    info = state.info
    if info.loaded:
        log.info("config file %s loaded: %s", info.path, ", ".join(sorted(state.values)) or "-")
    if info.error:
        log.warning("config file %s: %s", info.path, info.error)
    for warning in info.warnings:
        log.warning("config file %s: %s", info.path, warning)


def clear_cache() -> None:
    with _lock:
        _cache.clear()
