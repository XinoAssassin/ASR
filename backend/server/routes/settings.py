"""Settings (PLAN §5.3). Owned by ``backend-jobs`` (M1).

Secrets are returned masked only (rule 3); env-only values are not updatable (D7).
"""

from fastapi import APIRouter, HTTPException, status

from server.schemas import ConnectionTestResult, LocalModelOut, SettingsOut, SettingsUpdate

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("", response_model=SettingsOut)
async def get_settings() -> SettingsOut:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)


@router.put("", response_model=SettingsOut)
async def update_settings(body: SettingsUpdate) -> SettingsOut:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)


@router.post("/test-cloud", response_model=ConnectionTestResult)
async def test_cloud() -> ConnectionTestResult:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)


@router.get("/local-models", response_model=list[LocalModelOut])
async def list_local_models() -> list[LocalModelOut]:
    """Static list; every entry ``downloaded=false`` until M6."""
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED)
