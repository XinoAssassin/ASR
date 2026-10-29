"""Settings (PLAN §5.3). Owned by ``backend-jobs`` (M1).

Secrets are returned masked only (rule 3); env-only values are not updatable (D7).
"""

import logging

from fastapi import APIRouter, HTTPException, status
from fastapi.concurrency import run_in_threadpool

from asr.providers import cloud as cloud_provider
from asr.providers.local import models as local_models
from server import config
from server.config import EffectiveSettings, EnvSettings, load_effective_settings, mask_secret
from server.deps import DbDep, EnvDep
from server.jobs import MISSING_KEY
from server.schemas import (
    CloudSettingsOut,
    ConnectionTestResult,
    LocalModelOut,
    LocalSettingsOut,
    SettingsOut,
    SettingsUpdate,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])
log = logging.getLogger(__name__)

_422 = status.HTTP_422_UNPROCESSABLE_CONTENT

_KIND = {"asr": "asr", "align": "aligner", "vad": "vad", "diarize": "diarization"}


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
        ),
    )


@router.get("", response_model=SettingsOut)
async def get_settings(db: DbDep, env: EnvDep) -> SettingsOut:
    return _settings_out(env, await load_effective_settings(env, db))


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
            writes[config.SETTING_LOCAL_DIARIZE_MODEL] = body.local.diarize_model.strip()
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
    return _settings_out(env, await load_effective_settings(env, db))


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


@router.get("/local-models", response_model=list[LocalModelOut])
async def list_local_models(env: EnvDep) -> list[LocalModelOut]:
    """Registry models and whether they are in ``ASR_MODEL_CACHE_DIR`` (`asr models download`)."""

    def scan() -> list[LocalModelOut]:
        cache = env.model_cache_dir
        return [
            LocalModelOut(
                id=key,
                name=spec.repo_id,
                kind=_KIND.get(spec.kind, "asr"),  # type: ignore[arg-type]
                size_mb=local_models.disk_size_mb(cache, key)
                if local_models.is_downloaded(cache, key)
                else None,
                downloaded=local_models.is_downloaded(cache, key),
            )
            for key, spec in local_models.MODELS.items()
        ]

    return await run_in_threadpool(scan)
