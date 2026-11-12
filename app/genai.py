"""Self-service LiteLLM users and API keys for AI Factory users."""

import asyncio
import logging
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.litellm import (
    LiteLLMClient,
    LiteLLMConfigurationError,
    LiteLLMConnectionError,
    config_from_record,
    user_team_ids,
)
from app.models.genai_account import GenAiAccount
from app.models.genai_settings import GenAiSettings
from app.models.jupyter_ai_settings import JupyterAiSettings
from app.models.user import User
from app.schemas.genai import (
    GenAiAccountStatus,
    GenAiDailyUsage,
    GenAiIssuedKey,
    GenAiUsage,
    GenAiUsageHistory,
)
from app.security.secrets import SecretDecryptionError, decrypt_secret, encrypt_secret

logger = logging.getLogger("aifactory.genai")

_USER_ID = re.compile(r"^[a-z0-9][a-z0-9._@-]{0,127}$")
_locks: dict[int, asyncio.Lock] = {}


class GenAiUnavailable(RuntimeError):
    """GenAI is disabled or not fully configured by an administrator."""


class GenAiConflict(RuntimeError):
    pass


def litellm_user_id(user: User) -> str:
    value = (user.username or "").strip().lower()
    if not _USER_ID.fullmatch(value):
        raise GenAiConflict(
            "Kullanıcı adınız LiteLLM kullanıcı kimliği olarak kullanılamıyor."
        )
    return value


def _lock_for(user_id: int) -> asyncio.Lock:
    lock = _locks.get(user_id)
    if lock is None:
        lock = _locks[user_id] = asyncio.Lock()
    return lock


async def _settings(db: AsyncSession) -> GenAiSettings | None:
    return await db.get(GenAiSettings, 1)


def is_configured(record: GenAiSettings | None) -> bool:
    return bool(
        record and record.enabled and record.base_url and record.encrypted_admin_key
    )


async def _client(db: AsyncSession) -> tuple[LiteLLMClient, GenAiSettings]:
    record = await _settings(db)
    if not is_configured(record):
        raise GenAiUnavailable("GenAI erişimi henüz yönetici tarafından yapılandırılmadı.")
    try:
        return LiteLLMClient(config_from_record(record)), record
    except LiteLLMConfigurationError as exc:
        raise GenAiUnavailable(str(exc)) from exc


async def _account(db: AsyncSession, user: User) -> GenAiAccount | None:
    return (
        await db.execute(select(GenAiAccount).where(GenAiAccount.user_id == user.id))
    ).scalar_one_or_none()


def _key_alias(user_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"aifactory-{user_id}-{stamp}"


def _token_of(payload: dict) -> str:
    return str(payload.get("token_id") or payload.get("token") or "")


def _usage(info: dict) -> GenAiUsage:
    def number(value):
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    return GenAiUsage(
        spend=number(info.get("spend")) or 0.0,
        max_budget=number(info.get("max_budget")),
        budget_duration=info.get("budget_duration") or None,
        budget_reset_at=str(info["budget_reset_at"]) if info.get("budget_reset_at") else None,
    )


def _key_active(payload: dict, token: str) -> bool | None:
    keys = payload.get("keys")
    if not token or not isinstance(keys, list):
        return None
    for entry in keys:
        if isinstance(entry, dict) and token in {
            str(entry.get("token") or ""),
            str(entry.get("token_id") or ""),
        }:
            return True
    return False


class _Teams:
    """LiteLLM team lookups for one operation (one ``/team/list`` call)."""

    def __init__(self, client: LiteLLMClient):
        self.client = client
        self._by_ref: dict[str, dict] | None = None

    async def _load(self) -> dict[str, dict]:
        if self._by_ref is None:
            self._by_ref = {}
            for team in await self.client.list_teams():
                self._by_ref[str(team["team_id"])] = team
                if team.get("team_alias"):
                    self._by_ref.setdefault(str(team["team_alias"]), team)
        return self._by_ref

    async def resolve(self, ref: str) -> dict | None:
        if not ref:
            return None
        return (await self._load()).get(ref)

    async def alias(self, team_id: str) -> str:
        """Display name for a team id; falls back to the id on any lookup error."""
        if not team_id:
            return ""
        try:
            team = (await self._load()).get(team_id)
        except LiteLLMConnectionError:
            return team_id
        return str((team or {}).get("team_alias") or team_id)

    async def for_keys(self, user_payload: dict | None) -> str:
        """Pick the team new keys bind to: highest configured tier first."""
        member_of = user_team_ids(user_payload)
        if not member_of:
            return ""
        config = self.client.config
        for ref in config.team_priority:
            team = await self.resolve(ref)
            if team and str(team["team_id"]) in member_of:
                return str(team["team_id"])
        if len(member_of) == 1:
            return member_of[0]
        default = await self.resolve(config.default_team)
        if default and str(default["team_id"]) in member_of:
            return str(default["team_id"])
        return ""


async def account_status(db: AsyncSession, user: User) -> GenAiAccountStatus:
    record = await _settings(db)
    if not is_configured(record):
        return GenAiAccountStatus(configured=False)
    account = await _account(db, user)
    status = GenAiAccountStatus(
        configured=True,
        base_url=record.base_url,
        provisioned=bool(account and account.personal_key_alias),
        key_alias=account.personal_key_alias if account else "",
        created_at=account.created_at if account else None,
        rotated_at=account.rotated_at if account else None,
        workspace_key=bool(account and account.encrypted_workspace_key),
    )
    try:
        status.litellm_user_id = (
            account.litellm_user_id if account else litellm_user_id(user)
        )
        client, _record = await _client(db)
        payload = await client.get_user(status.litellm_user_id)
        teams = _Teams(client)
        if payload is not None:
            current = await teams.for_keys(payload)
            status.team = await teams.alias(current)
            if account and account.personal_key_alias:
                status.key_team = await teams.alias(account.personal_key_team)
                status.key_team_current = account.personal_key_team == current
    except (GenAiUnavailable, GenAiConflict, LiteLLMConnectionError) as exc:
        status.error = str(exc)
        return status
    status.litellm_user_exists = payload is not None
    if payload is not None:
        status.usage = _usage(payload.get("user_info") or {})
        if account:
            status.key_active = _key_active(payload, account.personal_key_token)
    return status


async def _ensure_litellm_user(
    client: LiteLLMClient, teams: _Teams, user_id: str, email: str | None
) -> dict:
    """Return the LiteLLM user, creating it (in the default team) if needed."""
    payload = await client.get_user(user_id)
    if payload is None:
        try:
            await client.create_user(user_id, email)
        except LiteLLMConnectionError as exc:
            # A concurrent or manual creation is fine; adopt the existing user.
            if await client.get_user(user_id) is None:
                if not (email and "email" in str(exc).lower()):
                    raise
                # The e-mail belongs to another LiteLLM user (e.g. a UI login).
                await client.create_user(user_id, None)
        payload = await client.get_user(user_id)
    if not user_team_ids(payload) and client.config.default_team:
        # New users, and adopted users without any team, join the default tier.
        team = await teams.resolve(client.config.default_team)
        if team is None:
            raise GenAiConflict(
                f"Varsayılan LiteLLM takımı bulunamadı: {client.config.default_team}"
            )
        await client.add_team_member(str(team["team_id"]), user_id)
        payload = await client.get_user(user_id)
    if payload is None:
        raise LiteLLMConnectionError("LiteLLM kullanıcısı oluşturulamadı.")
    return payload


async def _issue(
    client: LiteLLMClient, teams: _Teams, user_payload: dict, user_id: str, purpose: str
) -> tuple[dict, str]:
    team_id = await teams.for_keys(user_payload)
    suffix = "-workspace" if purpose == "workspace" else ""
    issued = await client.generate_key(
        user_id, _key_alias(user_id) + suffix, purpose, team_id=team_id
    )
    return issued, team_id


async def provision(db: AsyncSession, user: User) -> GenAiIssuedKey:
    """Create (or adopt) the LiteLLM user and issue a personal key once."""
    async with _lock_for(user.id):
        client, record = await _client(db)
        account = await _account(db, user)
        if account and account.personal_key_alias:
            raise GenAiConflict(
                "GenAI erişiminiz zaten etkin. Yeni anahtar için anahtarı yenileyin."
            )
        user_id = account.litellm_user_id if account else litellm_user_id(user)
        teams = _Teams(client)
        payload = await _ensure_litellm_user(client, teams, user_id, user.email)
        issued, team_id = await _issue(client, teams, payload, user_id, "personal")
        token = _token_of(issued)
        if account is None:
            account = GenAiAccount(user_id=user.id, litellm_user_id=user_id)
        account.personal_key_alias = str(issued.get("key_alias") or "")
        account.personal_key_token = token
        account.personal_key_team = team_id
        db.add(account)
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            await _discard_key(client, token or issued["key"])
            raise GenAiConflict("GenAI erişiminiz başka bir istekte oluşturuldu.") from exc
        except Exception:
            await db.rollback()
            await _discard_key(client, token or issued["key"])
            raise
        logger.info("Issued LiteLLM personal key %s for %s", account.personal_key_alias, user_id)
        return GenAiIssuedKey(
            api_key=issued["key"],
            key_alias=account.personal_key_alias,
            base_url=record.base_url,
            litellm_user_id=user_id,
            team=await teams.alias(team_id),
        )


async def rotate(db: AsyncSession, user: User) -> GenAiIssuedKey:
    """Issue a new personal key (bound to the current team), then delete the old one."""
    async with _lock_for(user.id):
        client, record = await _client(db)
        account = await _account(db, user)
        if account is None:
            raise GenAiConflict("Önce GenAI erişimi oluşturun.")
        user_id = account.litellm_user_id
        teams = _Teams(client)
        # The LiteLLM user may have been removed manually since provisioning.
        payload = await _ensure_litellm_user(client, teams, user_id, user.email)
        previous_token = account.personal_key_token
        previous_alias = account.personal_key_alias
        issued, team_id = await _issue(client, teams, payload, user_id, "personal")
        token = _token_of(issued)
        account.personal_key_alias = str(issued.get("key_alias") or "")
        account.personal_key_token = token
        account.personal_key_team = team_id
        account.rotated_at = datetime.now(timezone.utc)
        db.add(account)
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            await _discard_key(client, token or issued["key"])
            raise
        warning = None
        if not await _discard_key(client, previous_token, previous_alias):
            warning = (
                "Eski anahtar LiteLLM'de silinemedi; bir yöneticiden silmesini isteyin."
            )
        logger.info("Rotated LiteLLM personal key for %s", user_id)
        return GenAiIssuedKey(
            api_key=issued["key"],
            key_alias=account.personal_key_alias,
            base_url=record.base_url,
            litellm_user_id=user_id,
            warning=warning,
            team=await teams.alias(team_id),
        )


def _same_gateway(left: str, right: str) -> bool:
    def normalize(value: str) -> tuple[str, str, int | None, str]:
        parsed = urlsplit((value or "").strip())
        port = parsed.port or {"http": 80, "https": 443}.get(parsed.scheme)
        return (parsed.scheme, (parsed.hostname or "").lower(), port, parsed.path.rstrip("/"))

    return bool(left and right) and normalize(left) == normalize(right)


async def _workspace_ai_uses_genai_gateway(db: AsyncSession, record: GenAiSettings) -> bool:
    workspace_ai = await db.get(JupyterAiSettings, 1)
    if not workspace_ai or not workspace_ai.enabled:
        return False
    if not _same_gateway(workspace_ai.gateway_url, record.base_url):
        logger.warning(
            "GenAI and Workspace AI use different LiteLLM URLs; using the shared key."
        )
        return False
    return True


async def workspace_gateway_token(db: AsyncSession, user_id: int) -> str:
    """Return the user's own LiteLLM key for a new workspace, or "" for the shared key.

    Users with a GenAI account (opted in on the GenAI tab, or provisioned
    automatically at directory login) get a personal workspace key, but only
    when GenAI and Workspace AI point at the same LiteLLM. Any failure falls
    back to the shared key; workspace creation never fails here.
    """
    try:
        record = await _settings(db)
        if not is_configured(record):
            return ""
        account = (
            await db.execute(select(GenAiAccount).where(GenAiAccount.user_id == user_id))
        ).scalar_one_or_none()
        if account is None:
            return ""
        if not await _workspace_ai_uses_genai_gateway(db, record):
            return ""
        async with _lock_for(user_id):
            return await _ensure_workspace_key(db, record, account)
    except Exception as exc:  # noqa: BLE001 - never block workspace creation
        logger.warning("Per-user workspace key unavailable, using the shared key: %s", exc)
        return ""


async def auto_provision(db: AsyncSession, user: User) -> None:
    """Give a directory (AD) user a LiteLLM user and a workspace key at login.

    Admins turn this off with ``GenAiSettings.auto_provision_directory_users``.

    Runs once per user: after the workspace key exists, later logins make no
    LiteLLM calls (workspace creation keeps the key current). The personal key
    stays opt-in on the GenAI tab because it is shown only once. Never raises;
    a LiteLLM problem must not block the login and is retried next time.
    """
    if user.auth_source != "active_directory":
        return
    user_pk, email = user.id, user.email
    try:
        record = await _settings(db)
        if not is_configured(record) or not record.auto_provision_directory_users:
            return
        account = await _account(db, user)
        if account and account.encrypted_workspace_key:
            return
        async with _lock_for(user_pk):
            account = await _account(db, user)
            if account is None:
                client = LiteLLMClient(config_from_record(record))
                uid = litellm_user_id(user)
                await _ensure_litellm_user(client, _Teams(client), uid, email)
                account = GenAiAccount(user_id=user_pk, litellm_user_id=uid)
                db.add(account)
                try:
                    await db.commit()
                except IntegrityError:
                    # Another login or the GenAI tab created it concurrently.
                    await db.rollback()
                    return
                logger.info("Created LiteLLM user %s at directory login", uid)
            elif account.encrypted_workspace_key:
                return
            if await _workspace_ai_uses_genai_gateway(db, record):
                await _ensure_workspace_key(db, record, account)
    except Exception as exc:  # noqa: BLE001 - never block the login
        await db.rollback()
        logger.warning("GenAI auto-provisioning failed for user %s: %s", user_pk, exc)


async def _ensure_workspace_key(
    db: AsyncSession, record: GenAiSettings, account: GenAiAccount
) -> str:
    stored = ""
    if account.encrypted_workspace_key:
        try:
            stored = decrypt_secret(account.encrypted_workspace_key)
        except SecretDecryptionError:
            stored = ""
    client = LiteLLMClient(config_from_record(record))
    teams = _Teams(client)
    try:
        payload = await client.get_user(account.litellm_user_id)
    except LiteLLMConnectionError:
        # LiteLLM is down: keep using the stored key rather than the shared one.
        return stored
    if payload is None:
        return ""
    team_id = await teams.for_keys(payload)
    active = _key_active(payload, account.workspace_key_token)
    if stored and account.workspace_key_team == team_id and active is not False:
        return stored
    # Missing, unreadable, deleted in LiteLLM, or bound to an old tier.
    issued, team_id = await _issue(client, teams, payload, account.litellm_user_id, "workspace")
    previous = (account.workspace_key_token, account.workspace_key_alias)
    account.workspace_key_alias = str(issued.get("key_alias") or "")
    account.workspace_key_token = _token_of(issued)
    account.workspace_key_team = team_id
    account.encrypted_workspace_key = encrypt_secret(issued["key"])
    db.add(account)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        await _discard_key(client, _token_of(issued) or issued["key"])
        raise
    if any(previous):
        await _discard_key(client, *previous)
    logger.info("Issued LiteLLM workspace key %s", account.workspace_key_alias)
    return issued["key"]


async def usage_history(db: AsyncSession, user: User, days: int = 30) -> GenAiUsageHistory:
    account = await _account(db, user)
    if account is None:
        return GenAiUsageHistory(available=False)
    try:
        client, _record = await _client(db)
        end = date.today()
        entries = await client.daily_activity(
            account.litellm_user_id, end - timedelta(days=days - 1), end
        )
    except (GenAiUnavailable, LiteLLMConnectionError, ValueError, TypeError) as exc:
        logger.info("LiteLLM daily activity unavailable: %s", exc)
        return GenAiUsageHistory(available=False)
    return GenAiUsageHistory(
        available=True, days=[GenAiDailyUsage(**entry) for entry in entries]
    )


async def _discard_key(client: LiteLLMClient, token: str, alias: str = "") -> bool:
    if not token and not alias:
        return False
    try:
        await client.delete_key(token=token, alias=alias)
        return True
    except (LiteLLMConnectionError, LiteLLMConfigurationError) as exc:
        logger.warning("Could not delete LiteLLM key: %s", exc)
        return False


async def assistant_gateway(db: AsyncSession, user: User) -> tuple[str, str, bool | str]:
    """LiteLLM URL, the user's own workspace key and the TLS setting.

    The in-app assistant spends on the same per-user key that AI Factory
    issues and stores for the user's workspaces, so the user's LiteLLM
    budget and team limits apply. There is no shared-key fallback.
    """
    record = await _settings(db)
    if not is_configured(record):
        raise GenAiUnavailable("GenAI (LiteLLM) bağlantısı yapılandırılmamış.")
    account = await _account(db, user)
    if account is None:
        raise GenAiUnavailable(
            "Asistanı kullanmak için önce GenAI sekmesinden hesabınızı oluşturun."
        )
    async with _lock_for(user.id):
        key = await _ensure_workspace_key(db, record, account)
    if not key:
        raise GenAiUnavailable("LiteLLM kullanıcınız bulunamadı; GenAI sekmesini kontrol edin.")
    verify: bool | str = record.ca_cert_file or record.validate_tls
    return record.base_url.rstrip("/"), key, verify
