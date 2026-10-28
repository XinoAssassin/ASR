"""Settings (PLAN §5.3). Owned by ``backend-jobs`` (M1).

Secrets are returned masked only (rule 3); env-only values are not updatable (D7).
"""

import logging

from fastapi import APIRouter, HTTPException, status
from fastapi.concurrency import run_in_threadpool

from asr.providers import cloud as cloud_provider
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

LOCAL_MODELS: tuple[LocalModelOut, ...] = (
    LocalModelOut(
        id="Qwen/Qwen3-ASR-1.7B", name="Qwen3-ASR 1.7B", kind="asr", size_mb=None, downloaded=False
    ),
    LocalModelOut(
        id="Qwen/Qwen3-ASR-0.6B", name="Qwen3-ASR 0.6B", kind="asr", size_mb=None, downloaded=False
    ),
    LocalModelOut(
        id="FunAudioLLM/Fun-ASR-Nano-2512",
        name="Fun-ASR-Nano 2512",
        kind="asr",
        size_mb=None,
        downloaded=False,
    ),
    LocalModelOut(
        id="Qwen/Qwen3-ForcedAligner-0.6B",
        name="Qwen3 Forced Aligner 0.6B",
        kind="aligner",
        size_mb=None,
        downloaded=False,
    ),
    LocalModelOut(
        id="iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
        name="FSMN-VAD",
        kind="vad",
        size_mb=None,
        downloaded=False,
    ),
    LocalModelOut(
        id="iic/speech_campplus_sv_zh-cn_16k-common",
        name="CAM++",
        kind="diarization",
        size_mb=None,
        downloaded=False,
    ),
)


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
            writes[config.SETTING_LOCAL_ASR_MODEL] = body.local.asr_model.strip()
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
async def list_local_models() -> list[LocalModelOut]:
    """Static list; every entry ``downloaded=false`` until M6."""
    return list(LOCAL_MODELS)
