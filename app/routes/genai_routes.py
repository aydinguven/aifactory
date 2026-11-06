import json
import time
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app import genai
from app.genai_stats import usage_stats
from app.auth.dependencies import get_current_admin_user, get_current_user
from app.database import get_db
from app.integrations.litellm import (
    LiteLLMClient,
    LiteLLMConfigurationError,
    LiteLLMConnectionError,
    config_from_record,
    parse_models,
)
from app.models.genai_settings import GenAiSettings
from app.models.user import User, UserRole
from app.schemas.genai import (
    GenAiAccountStatus,
    GenAiIssuedKey,
    GenAiSettingsOut,
    GenAiSettingsUpdate,
    GenAiTestResult,
    GenAiUsageHistory,
)
from app.security.secrets import encrypt_secret

genai_router = APIRouter(prefix="/api/genai", tags=["GenAI"])
genai_admin_router = APIRouter(prefix="/api/admin/genai-settings", tags=["GenAI"])


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, genai.GenAiUnavailable):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, genai.GenAiConflict):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=502, detail=str(exc))


@genai_router.get("/account", response_model=GenAiAccountStatus)
async def get_account(
    response: Response,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Describe the caller's LiteLLM user, key and spend without any secret."""
    _no_store(response)
    return await genai.account_status(db, current_user)


@genai_router.post("/account", response_model=GenAiIssuedKey)
async def create_account(
    response: Response,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create or adopt the caller's LiteLLM user and return a key exactly once."""
    _no_store(response)
    try:
        return await genai.provision(db, current_user)
    except (genai.GenAiUnavailable, genai.GenAiConflict, LiteLLMConnectionError) as exc:
        raise _http_error(exc) from exc


@genai_router.post("/account/rotate", response_model=GenAiIssuedKey)
async def rotate_account_key(
    response: Response,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Replace the caller's personal key; spend history stays on the user."""
    _no_store(response)
    try:
        return await genai.rotate(db, current_user)
    except (genai.GenAiUnavailable, genai.GenAiConflict, LiteLLMConnectionError) as exc:
        raise _http_error(exc) from exc


@genai_router.get("/usage", response_model=GenAiUsageHistory)
async def get_usage(
    response: Response,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    _no_store(response)
    return await genai.usage_history(db, current_user)


def _without_spend(value):
    if isinstance(value, dict):
        return {k: _without_spend(v) for k, v in value.items() if k != "spend"}
    if isinstance(value, list):
        return [_without_spend(item) for item in value]
    return value


@genai_router.get("/stats")
async def get_stats(
    response: Response,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    days: int = 30,
):
    """Usage leaderboards and charts; spend (USD) is shown to admins only."""
    _no_store(response)
    stats = await usage_stats(db, days, viewer=current_user)
    is_admin = current_user.role == UserRole.ADMIN
    stats["show_spend"] = is_admin
    return stats if is_admin else _without_spend(stats)


def _settings_out(record: GenAiSettings | None) -> GenAiSettingsOut:
    if record is None:
        return GenAiSettingsOut(
            managed=False,
            enabled=False,
            base_url="",
            has_admin_key=False,
            validate_tls=True,
            ca_cert_file="",
            timeout_seconds=15,
            user_role="",
            models=[],
            max_budget=None,
            budget_duration="",
            key_duration="",
        )
    return GenAiSettingsOut(
        managed=True,
        enabled=record.enabled,
        base_url=record.base_url,
        has_admin_key=bool(record.encrypted_admin_key),
        validate_tls=record.validate_tls,
        ca_cert_file=record.ca_cert_file,
        timeout_seconds=record.timeout_seconds,
        user_role=record.user_role,
        models=parse_models(record.models_json),
        max_budget=record.max_budget,
        budget_duration=record.budget_duration,
        key_duration=record.key_duration,
        default_team=record.default_team,
        team_priority=parse_models(record.team_priority_json),
        auto_provision_directory_users=record.auto_provision_directory_users,
        updated_at=record.updated_at,
    )


@genai_admin_router.get("", response_model=GenAiSettingsOut)
async def get_genai_settings(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return _settings_out(await db.get(GenAiSettings, 1))


@genai_admin_router.put("", response_model=GenAiSettingsOut)
async def update_genai_settings(
    update: GenAiSettingsUpdate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Store the LiteLLM connection; the admin key is encrypted and write-only."""
    record = await db.get(GenAiSettings, 1)
    has_key = bool(record and record.encrypted_admin_key)
    if update.enabled and (
        update.admin_key == "" or (update.admin_key is None and not has_key)
    ):
        raise HTTPException(
            status_code=422,
            detail="GenAI etkinleştirildiğinde LiteLLM yönetici API anahtarı zorunludur.",
        )
    if record is None:
        record = GenAiSettings(id=1)
    record.enabled = update.enabled
    record.base_url = update.base_url
    record.validate_tls = update.validate_tls
    record.ca_cert_file = update.ca_cert_file
    record.timeout_seconds = update.timeout_seconds
    record.user_role = update.user_role
    record.models_json = json.dumps(update.models, ensure_ascii=False)
    record.max_budget = update.max_budget
    record.budget_duration = update.budget_duration
    record.key_duration = update.key_duration
    record.default_team = update.default_team
    record.team_priority_json = json.dumps(update.team_priority, ensure_ascii=False)
    record.auto_provision_directory_users = update.auto_provision_directory_users
    if update.admin_key is not None:
        record.encrypted_admin_key = encrypt_secret(update.admin_key)
    record.updated_at = datetime.now(timezone.utc)
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return _settings_out(record)


@genai_admin_router.post("/test", response_model=GenAiTestResult)
async def test_genai_settings(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Check the saved URL and that the saved key can manage LiteLLM users."""
    record = await db.get(GenAiSettings, 1)
    if record is None or not record.base_url:
        return GenAiTestResult(ok=False, message="Önce LiteLLM adresini kaydedin.")
    started = time.monotonic()
    try:
        client = LiteLLMClient(config_from_record(record))
        identity = await client.whoami()
    except (LiteLLMConfigurationError, LiteLLMConnectionError) as exc:
        return GenAiTestResult(ok=False, message=str(exc))
    latency_ms = int((time.monotonic() - started) * 1000)
    is_admin = identity["user_role"] == "proxy_admin"
    missing_teams: list[str] = []
    refs = [record.default_team, *parse_models(record.team_priority_json)]
    refs = [ref for ref in dict.fromkeys(refs) if ref]
    if is_admin:
        if refs:
            try:
                teams = await client.list_teams()
            except LiteLLMConnectionError as exc:
                return GenAiTestResult(ok=False, message=f"Takımlar okunamadı: {exc}")
            known = {str(t["team_id"]) for t in teams} | {
                str(t["team_alias"]) for t in teams if t.get("team_alias")
            }
            missing_teams = [ref for ref in refs if ref not in known]
    ok = is_admin and not missing_teams
    if not is_admin:
        message = "Bağlantı başarılı ancak anahtarın sahibi proxy_admin rolünde değil."
    elif missing_teams:
        message = "LiteLLM'de bulunamayan takımlar: " + ", ".join(missing_teams)
    elif refs:
        message = "LiteLLM bağlantısı, yönetici yetkisi ve takımlar doğrulandı."
    else:
        message = "LiteLLM bağlantısı ve yönetici yetkisi doğrulandı."
    return GenAiTestResult(
        ok=ok,
        message=message,
        admin_user_id=identity["user_id"],
        admin_role=identity["user_role"],
        latency_ms=latency_ms,
    )
