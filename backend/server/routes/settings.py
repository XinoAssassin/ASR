"""Settings (PLAN §5.3). Owned by ``backend-jobs`` (M1).

Secrets are returned masked only (rule 3); env-only values are not updatable (D7). The online
settings (``cloud.*``, ``llm.*``) are layered settings table > config file > env > default
(``server.config.effective_settings``); ``PUT`` writes only the settings-table layer.
"""

import asyncio
import logging
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool

from asr.correct import llm as llm_core
from asr.providers import cloud as cloud_provider
from asr.providers.cloud.security import redact
from asr.providers.local import models as local_models
from server import config
from server import config_file as cfgfile
from server.config import (
    EffectiveSettings,
    EnvSettings,
    effective_settings,
    load_effective_settings,
    mask_secret,
)
from server.db import Database
from server.deps import DbDep, EnvDep, ModelDownloadsDep
from server.jobs import MISSING_KEY
from server.model_downloads import DownloadBusy, ModelDownloads
from server.schemas import (
    CloudSettingsOut,
    ConfigFileOut,
    ConnectionTestResult,
    EnvironmentOut,
    LlmSettingsOut,
    LlmTestOut,
    LocalModelOut,
    LocalSettingsOut,
    SettingsOut,
    SettingsUpdate,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])
log = logging.getLogger(__name__)

_422 = status.HTTP_422_UNPROCESSABLE_CONTENT

_KIND = {"asr": "asr", "align": "aligner", "vad": "vad", "diarize": "diarization"}


# Response fields only the environment sets (dotted path -> variable), for the UI (§4-f).
ENV_ONLY_FIELDS: dict[str, str] = {
    "api_key_required": "ASR_API_KEY",
    "upload_roots": "ASR_UPLOAD_ROOTS",
    "local.model_cache_dir": "ASR_MODEL_CACHE_DIR",
    "environment.api_key_required": "ASR_API_KEY",
    "environment.allow_unauthenticated_network": "ASR_ALLOW_UNAUTHENTICATED_NETWORK",
    "environment.cors_origins": "ASR_CORS_ORIGINS",
    "environment.allowed_hosts": "ASR_ALLOWED_HOSTS",
    "environment.upload_roots": "ASR_UPLOAD_ROOTS",
    "environment.data_dir": "ASR_DATA_DIR",
    "environment.model_cache_dir": "ASR_MODEL_CACHE_DIR",
    "environment.decoder": "ASR_DECODER",
    "environment.torch_threads": "ASR_TORCH_THREADS",
    "environment.local_dtype": "ASR_LOCAL_DTYPE",
    "environment.llm_model": "ASR_LLM_MODEL",
    "environment.log_level": "ASR_LOG_LEVEL",
    "environment.log_format": "ASR_LOG_FORMAT",
    "environment.log_file": "ASR_LOG_FILE",
}


def _environment_out(env: EnvSettings) -> EnvironmentOut:
    return EnvironmentOut(
        api_key_required=env.api_key_required,
        allow_unauthenticated_network=env.allow_unauthenticated_network,
        cors_origins=list(env.cors_origins),
        allowed_hosts=list(env.allowed_hosts),
        upload_roots=[str(r) for r in env.upload_roots],
        data_dir=str(env.data_dir),
        model_cache_dir=str(env.model_cache_dir),
        decoder=env.decoder,
        torch_threads=env.torch_threads,
        local_dtype=env.local_dtype,
        llm_model=env.llm_model,
        log_level=env.log_level,
        log_format=env.log_format,
        log_file=None if env.log_file is None else str(env.log_file),
    )


def _api_key_source(eff: EffectiveSettings) -> Literal["env", "config", "settings"] | None:
    if not eff.cloud_api_key_set:
        return None
    source = eff.sources.get(config.SETTING_CLOUD_API_KEY)
    return source if source in ("env", "config", "settings") else None  # type: ignore[return-value]


async def _current(env: EnvSettings, db: Database) -> SettingsOut:
    return _settings_out(env, await load_effective_settings(env, db))


def _settings_out(env: EnvSettings, eff: EffectiveSettings) -> SettingsOut:
    return SettingsOut(
        provider_default=eff.provider_default,
        api_key_required=env.api_key_required,
        upload_roots=[str(r) for r in env.upload_roots],
        local=LocalSettingsOut(
            asr_model=eff.local_asr_model,
            diarize_model=eff.local_diarize_model,
            device=eff.local_device,
            model_cache_dir=str(env.model_cache_dir),
        ),
        cloud=CloudSettingsOut(
            endpoint=eff.cloud_endpoint,
            model=eff.cloud_model,
            file_transfer=eff.cloud_file_transfer,
            api_key_set=eff.cloud_api_key_set,
            api_key_masked=mask_secret(eff.cloud_api_key),
            api_key_source=_api_key_source(eff),
        ),
        llm=LlmSettingsOut(
            provider=eff.llm_provider,
            endpoint=eff.llm_endpoint,
            model=eff.llm_model,
            api_key_set=eff.llm_api_key_set,
            api_key_masked=mask_secret(eff.llm_api_key),
        ),
        sources=dict(eff.sources),
        config_file=ConfigFileOut(
            path=eff.config_file.path,
            loaded=eff.config_file.loaded,
            error=eff.config_file.error,
            warnings=list(eff.config_file.warnings),
        ),
        environment=_environment_out(env),
        env_only=dict(ENV_ONLY_FIELDS),
    )


def _key_value(secret: object, field: str) -> str:
    """A submitted key: ``""`` (clear) or a validated key; 422 without echoing it."""
    key = secret.get_secret_value().strip()  # type: ignore[attr-defined]
    if not key:
        return ""
    try:
        return cfgfile.valid_api_key(key)
    except ValueError:
        raise HTTPException(_422, f"{field} must not contain whitespace") from None


def _cloud_writes(body: SettingsUpdate) -> dict[str, str]:
    writes: dict[str, str] = {}
    cloud = body.cloud
    if cloud is None:
        return writes
    if cloud.endpoint is not None:
        endpoint = cloud.endpoint.strip().rstrip("/")
        if endpoint and not cloud_provider.is_allowed_endpoint(endpoint):
            raise HTTPException(
                _422,
                "cloud.endpoint must be one of the allowed DashScope endpoints: "
                + ", ".join(cloud_provider.ALLOWED_ENDPOINTS),
            )
        writes[config.SETTING_CLOUD_ENDPOINT] = endpoint
    if cloud.model is not None:
        model = cloud.model.strip()
        if model:
            try:
                model = cfgfile.valid_model(model)
            except ValueError as exc:
                raise HTTPException(_422, f"cloud.model {exc}") from None
        writes[config.SETTING_CLOUD_MODEL] = model
    if cloud.file_transfer is not None:
        writes[config.SETTING_CLOUD_FILE_TRANSFER] = cloud.file_transfer
    if cloud.api_key is not None:
        writes[config.SETTING_CLOUD_API_KEY] = _key_value(cloud.api_key, "cloud.api_key")
    return writes


def _llm_writes(
    body: SettingsUpdate,
    env: EnvSettings,
    file: cfgfile.ConfigFileState,
    overrides: dict[str, str],
    writes: dict[str, str],
) -> dict[str, str]:
    """Validated ``llm.*`` writes. The endpoint is checked against the provider that will be in
    effect after this request (whichever layer it comes from)."""
    out: dict[str, str] = {}
    llm = body.llm
    if llm is None:
        return out
    if llm.provider is not None:
        out[config.SETTING_LLM_PROVIDER] = llm.provider
    if llm.model is not None:
        model = llm.model.strip()
        if model:
            try:
                model = cfgfile.valid_model(model)
            except ValueError as exc:
                raise HTTPException(_422, f"llm.model {exc}") from None
        out[config.SETTING_LLM_MODEL] = model
    if llm.endpoint is not None:
        endpoint = llm.endpoint.strip()
        if endpoint:
            pending = _apply({**overrides}, {**writes, **out})
            provider = effective_settings(env, pending, file).llm_provider
            if provider == "dashscope":
                endpoint = endpoint.rstrip("/")
                if not cloud_provider.is_allowed_endpoint(endpoint):
                    raise HTTPException(
                        _422,
                        "llm.endpoint must be one of the allowed DashScope endpoints for "
                        "provider dashscope: " + ", ".join(cloud_provider.ALLOWED_ENDPOINTS),
                    )
            else:
                try:
                    endpoint = llm_core.normalize_base_url(endpoint)
                except ValueError as exc:
                    raise HTTPException(_422, f"llm.endpoint {exc}") from None
        out[config.SETTING_LLM_ENDPOINT] = endpoint
    if llm.api_key is not None:
        out[config.SETTING_LLM_API_KEY] = _key_value(llm.api_key, "llm.api_key")
    return out


def _apply(overrides: dict[str, str], writes: dict[str, str]) -> dict[str, str]:
    """``overrides`` with ``writes`` applied (``""`` deletes)."""
    for key, value in writes.items():
        if value:
            overrides[key] = value
        else:
            overrides.pop(key, None)
    return overrides


@router.get("", response_model=SettingsOut)
async def get_settings(db: DbDep, env: EnvDep) -> SettingsOut:
    return await _current(env, db)


@router.put("", response_model=SettingsOut)
async def update_settings(body: SettingsUpdate, db: DbDep, env: EnvDep) -> SettingsOut:
    """Partial update of the settings-table layer. For every field: omitted = unchanged; ``""``
    = clear the override, so the config file / env / default value applies again. Keys are
    write-only.

    The LLM key is bound to its host: when the effective LLM becomes an ``openai`` server with a
    different endpoint (or the provider switches to ``openai``) and the request carries no
    ``llm.api_key``, the stored LLM key is cleared."""
    writes: dict[str, str] = {}
    if body.provider_default is not None:
        writes[config.SETTING_PROVIDER_DEFAULT] = body.provider_default
    if body.local is not None:
        if body.local.asr_model is not None:
            asr_model = body.local.asr_model.strip()
            if asr_model and (
                asr_model not in local_models.MODELS or local_models.MODELS[asr_model].kind != "asr"
            ):
                raise HTTPException(_422, f"unknown local ASR model {asr_model!r}")
            writes[config.SETTING_LOCAL_ASR_MODEL] = asr_model
        if body.local.diarize_model is not None:
            diarize_model = body.local.diarize_model.strip()
            if diarize_model and (
                diarize_model not in local_models.MODELS
                or local_models.MODELS[diarize_model].kind != "diarize"
            ):
                raise HTTPException(_422, f"unknown local diarization model {diarize_model!r}")
            writes[config.SETTING_LOCAL_DIARIZE_MODEL] = diarize_model
        if body.local.device is not None:
            writes[config.SETTING_LOCAL_DEVICE] = body.local.device
    writes.update(_cloud_writes(body))
    overrides = await db.get_settings()
    file = await asyncio.to_thread(cfgfile.load, env)
    writes.update(_llm_writes(body, env, file, overrides, writes))

    if body.llm is None or body.llm.api_key is None:
        before = effective_settings(env, overrides, file)
        after = effective_settings(env, _apply(dict(overrides), writes), file)
        moved = before.llm_provider != "openai" or before.llm_endpoint != after.llm_endpoint
        if (
            after.llm_provider == "openai"
            and moved
            and config.SETTING_LLM_API_KEY in overrides
            and writes.get(config.SETTING_LLM_API_KEY) is None
        ):
            writes[config.SETTING_LLM_API_KEY] = ""

    for key_name, value in writes.items():
        if value:
            await db.set_setting(key_name, value)
        else:  # "" -> back to the config file / env / default
            await db.delete_setting(key_name)
    return await _current(env, db)


@router.post("/test-cloud", response_model=ConnectionTestResult)
async def test_cloud(db: DbDep, env: EnvDep) -> ConnectionTestResult:
    """Check the effective DashScope key against the effective endpoint."""
    eff = await load_effective_settings(env, db)
    if not eff.cloud_api_key_set:
        return ConnectionTestResult(ok=False, message=MISSING_KEY)
    try:
        return await run_in_threadpool(
            cloud_provider.test_connection,
            api_key=eff.cloud_api_key.get_secret_value(),
            endpoint=eff.cloud_endpoint,
            model=eff.cloud_model,
        )
    except Exception as exc:
        # Type only: an exception message could echo request details.
        log.error("cloud connection test raised %s", type(exc).__name__)
        return ConnectionTestResult(ok=False, message="Connection test failed unexpectedly")


@router.post("/test-llm", response_model=LlmTestOut)
async def test_llm(db: DbDep, env: EnvDep) -> LlmTestOut:
    """A tiny chat completion (``max_tokens`` 16) against the effective LLM config. Always 200;
    the message is redacted and never contains the key. Sends a few tokens to the configured
    server (DashScope: billed)."""
    eff = await load_effective_settings(env, db)
    kwargs = eff.llm_kwargs()
    secrets = (kwargs["api_key"], eff.cloud_api_key.get_secret_value())
    try:
        ok, message, model = await run_in_threadpool(llm_core.check_llm, **kwargs)
    except Exception as exc:
        log.error("LLM connection test raised %s", type(exc).__name__)
        return LlmTestOut(ok=False, message="LLM test failed unexpectedly", model=None)
    if not ok:
        hints = [w for w in eff.config_file.warnings if w.startswith("llm.")]
        if hints:
            message = f"{message} ({'; '.join(hints)})"
    return LlmTestOut(
        ok=ok,
        message=redact(message, secrets),
        model=None if model is None else redact(model, secrets, max_chars=200),
    )


def _model_row(cache: Path, downloads: ModelDownloads, key: str) -> LocalModelOut:
    spec = local_models.MODELS[key]
    status_, error = downloads.status(key)
    downloaded = status_ == "downloaded"
    return LocalModelOut(
        id=key,
        name=spec.repo_id,
        kind=_KIND.get(spec.kind, "asr"),  # type: ignore[arg-type]
        size_mb=local_models.disk_size_mb(cache, key) if downloaded else None,
        downloaded=downloaded,
        status=status_,
        error=error,
        has_mirror=spec.mirror is not None,
    )


@router.get("/local-models", response_model=list[LocalModelOut])
async def list_local_models(env: EnvDep, downloads: ModelDownloadsDep) -> list[LocalModelOut]:
    """Registry models and whether they are in ``ASR_MODEL_CACHE_DIR``."""

    def scan() -> list[LocalModelOut]:
        return [_model_row(env.model_cache_dir, downloads, key) for key in local_models.MODELS]

    return await run_in_threadpool(scan)


@router.post(
    "/local-models/{key}/download",
    response_model=LocalModelOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses={404: {"description": "Unknown model"}, 409: {"description": "Download running"}},
)
async def download_local_model(
    key: str,
    env: EnvDep,
    downloads: ModelDownloadsDep,
    mirror: Annotated[bool, Query(description="Fetch from the ModelScope mirror if any.")] = False,
) -> LocalModelOut:
    """Start an explicit background download of a registry model (AGENTS rule 6)."""
    if key not in local_models.MODELS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown model {key!r}")
    try:
        downloads.start(key, mirror=mirror)
    except DownloadBusy as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    return await run_in_threadpool(_model_row, env.model_cache_dir, downloads, key)
