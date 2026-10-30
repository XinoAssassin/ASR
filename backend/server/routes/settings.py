"""Settings (PLAN §5.3). Owned by ``backend-jobs`` (M1).

Secrets are returned masked only (rule 3); env-only values are not updatable (D7).
"""

import logging
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool

from asr.providers import cloud as cloud_provider
from asr.providers.local import models as local_models
from server import config
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
    ConnectionTestResult,
    EnvironmentOut,
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


def _api_key_source(
    eff: EffectiveSettings, overrides: dict[str, str]
) -> Literal["env", "settings"] | None:
    if not eff.cloud_api_key_set:
        return None
    stored = overrides.get(config.SETTING_CLOUD_API_KEY)
    return "settings" if stored and stored == eff.cloud_api_key.get_secret_value() else "env"


async def _current(env: EnvSettings, db: Database) -> SettingsOut:
    overrides = await db.get_settings()
    return _settings_out(env, effective_settings(env, overrides), overrides)


def _settings_out(
    env: EnvSettings, eff: EffectiveSettings, overrides: dict[str, str]
) -> SettingsOut:
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
            api_key_source=_api_key_source(eff, overrides),
        ),
        environment=_environment_out(env),
        env_only=dict(ENV_ONLY_FIELDS),
    )


@router.get("", response_model=SettingsOut)
async def get_settings(db: DbDep, env: EnvDep) -> SettingsOut:
    return await _current(env, db)


@router.put("", response_model=SettingsOut)
async def update_settings(body: SettingsUpdate, db: DbDep, env: EnvDep) -> SettingsOut:
    """Partial update; omitted fields keep their value. ``cloud.api_key`` is write-only:
    non-empty sets the override, ``""`` clears it (the env value applies again)."""
    writes: dict[str, str] = {}
    clear_key = False
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
    if body.cloud is not None:
        cloud = body.cloud
        if cloud.endpoint is not None:
            endpoint = cloud.endpoint.strip().rstrip("/")
            if not cloud_provider.is_allowed_endpoint(endpoint):
                raise HTTPException(
                    _422,
                    "cloud.endpoint must be one of the allowed DashScope endpoints: "
                    + ", ".join(cloud_provider.ALLOWED_ENDPOINTS),
                )
            writes[config.SETTING_CLOUD_ENDPOINT] = endpoint
        if cloud.model is not None:
            model = cloud.model.strip()
            if not model:
                raise HTTPException(_422, "cloud.model must not be blank")
            writes[config.SETTING_CLOUD_MODEL] = model
        if cloud.file_transfer is not None:
            writes[config.SETTING_CLOUD_FILE_TRANSFER] = cloud.file_transfer
        if cloud.api_key is not None:
            key = cloud.api_key.get_secret_value().strip()
            if not key:
                clear_key = True
            elif any(c.isspace() for c in key) or not key.isprintable():
                raise HTTPException(_422, "cloud.api_key must not contain whitespace")
            else:
                writes[config.SETTING_CLOUD_API_KEY] = key

    for key_name, value in writes.items():
        if value:
            await db.set_setting(key_name, value)
        else:  # e.g. blank local model name -> back to the default
            await db.delete_setting(key_name)
    if clear_key:
        await db.delete_setting(config.SETTING_CLOUD_API_KEY)
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
