"""Minimal async client for the LiteLLM proxy management API.

Every call authenticates with the admin key of a LiteLLM ``proxy_admin`` user.
Neither that key nor generated user keys may appear in error messages.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import date

import httpx

from app.models.genai_settings import GenAiSettings
from app.security.secrets import SecretDecryptionError, decrypt_secret

_SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{4,}")


class LiteLLMConfigurationError(ValueError):
    pass


class LiteLLMConnectionError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class LiteLLMConfig:
    enabled: bool
    base_url: str
    admin_key: str
    validate_tls: bool = True
    ca_cert_file: str = ""
    timeout_seconds: int = 15
    user_role: str = ""
    models: list[str] = field(default_factory=list)
    max_budget: float | None = None
    budget_duration: str = ""
    key_duration: str = ""
    default_team: str = ""
    team_priority: list[str] = field(default_factory=list)


def parse_models(models_json: str) -> list[str]:
    try:
        values = json.loads(models_json or "[]")
    except ValueError:
        return []
    if not isinstance(values, list):
        return []
    return [str(value) for value in values if isinstance(value, str) and value]


def config_from_record(record: GenAiSettings) -> LiteLLMConfig:
    try:
        admin_key = decrypt_secret(record.encrypted_admin_key)
    except SecretDecryptionError as exc:
        raise LiteLLMConfigurationError(
            "Kayıtlı LiteLLM yönetici anahtarı çözülemedi; yeniden kaydedin."
        ) from exc
    return LiteLLMConfig(
        enabled=record.enabled,
        base_url=record.base_url,
        admin_key=admin_key,
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
    )


def user_team_ids(user_payload: dict | None) -> list[str]:
    """Team ids from a ``/user/info`` payload (user_info.teams or teams[])."""
    if not user_payload:
        return []
    ids: list[str] = []
    info = user_payload.get("user_info") if isinstance(user_payload.get("user_info"), dict) else {}
    for value in info.get("teams") or []:
        if isinstance(value, str) and value not in ids:
            ids.append(value)
    for team in user_payload.get("teams") or []:
        if isinstance(team, dict) and team.get("team_id") and team["team_id"] not in ids:
            ids.append(str(team["team_id"]))
    return ids


def validate_config(config: LiteLLMConfig) -> None:
    if not config.base_url.startswith(("http://", "https://")):
        raise LiteLLMConfigurationError(
            "LiteLLM adresi http:// veya https:// ile başlamalıdır."
        )
    parsed = httpx.URL(config.base_url)
    if not parsed.host or parsed.username or parsed.password:
        raise LiteLLMConfigurationError(
            "Geçerli ve kullanıcı bilgisi içermeyen bir LiteLLM adresi girin."
        )
    if not config.admin_key:
        raise LiteLLMConfigurationError("LiteLLM yönetici API anahtarı tanımlı değil.")
    if config.ca_cert_file and not config.validate_tls:
        raise LiteLLMConfigurationError(
            "Özel CA kullanılırken TLS doğrulaması açık olmalıdır."
        )


def _scrub(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return _SECRET_PATTERN.sub("sk-***", text)


def _error_detail(response: httpx.Response, admin_key: str) -> str:
    detail = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or "")
            detail = detail or str(payload.get("detail") or payload.get("message") or "")
    except ValueError:
        detail = response.text.strip()
    compact = " ".join(_scrub(detail, admin_key).split())
    return f": {compact[:500]}" if compact else ""


class LiteLLMClient:
    def __init__(self, config: LiteLLMConfig):
        validate_config(config)
        self.config = config

    def _client(self) -> httpx.AsyncClient:
        verify: bool | str = self.config.validate_tls
        if self.config.ca_cert_file:
            verify = self.config.ca_cert_file
        return httpx.AsyncClient(
            base_url=self.config.base_url.rstrip("/") + "/",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.config.admin_key}",
            },
            verify=verify,
            timeout=self.config.timeout_seconds,
            follow_redirects=False,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        allow_list: bool = False,
    ) -> dict | list:
        try:
            async with self._client() as client:
                # A relative target keeps an optional reverse-proxy prefix.
                response = await client.request(
                    method, path.lstrip("/"), params=params, json=json
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            detail = _error_detail(exc.response, self.config.admin_key)
            raise LiteLLMConnectionError(
                f"LiteLLM isteği başarısız ({exc.response.status_code}){detail}",
                status_code=exc.response.status_code,
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            message = _scrub(str(exc), self.config.admin_key)
            raise LiteLLMConnectionError(
                f"LiteLLM'e bağlanılamadı: {message or type(exc).__name__}"
            ) from exc
        if not isinstance(payload, dict) and not (allow_list and isinstance(payload, list)):
            raise LiteLLMConnectionError("LiteLLM beklenmeyen bir yanıt döndürdü.")
        return payload

    async def get_user(self, user_id: str) -> dict | None:
        """Return ``/user/info`` for ``user_id`` or None when it does not exist."""
        try:
            payload = await self._request("GET", "/user/info", params={"user_id": user_id})
        except LiteLLMConnectionError as exc:
            message = str(exc).lower()
            if exc.status_code == 404 or (
                exc.status_code == 400
                and ("not found" in message or "does not exist" in message)
            ):
                return None
            raise
        info = payload.get("user_info")
        if not isinstance(info, dict) or not info:
            # Older LiteLLM releases answer 200 with an empty user_info.
            return None
        return payload

    async def create_user(
        self, user_id: str, user_email: str | None
    ) -> dict:
        body: dict = {
            "user_id": user_id,
            "auto_create_key": False,
            "metadata": {"source": "aifactory"},
        }
        if user_email:
            body["user_email"] = user_email
        if self.config.user_role:
            body["user_role"] = self.config.user_role
        # The budget lives on the user so it spans rotated and workspace keys.
        if self.config.max_budget is not None:
            body["max_budget"] = self.config.max_budget
        if self.config.budget_duration:
            body["budget_duration"] = self.config.budget_duration
        return await self._request("POST", "/user/new", json=body)

    async def generate_key(
        self, user_id: str, key_alias: str, purpose: str, team_id: str = ""
    ) -> dict:
        body: dict = {
            "user_id": user_id,
            "key_alias": key_alias,
            "metadata": {"source": "aifactory", "purpose": purpose},
        }
        if team_id:
            # Team model access, budgets and limits apply only to team keys.
            body["team_id"] = team_id
        if self.config.models:
            body["models"] = list(self.config.models)
        if self.config.key_duration:
            body["duration"] = self.config.key_duration
        payload = await self._request("POST", "/key/generate", json=body)
        if not isinstance(payload.get("key"), str) or not payload["key"]:
            raise LiteLLMConnectionError("LiteLLM anahtar döndürmedi.")
        return payload

    async def delete_key(self, token: str = "", alias: str = "") -> None:
        """Delete by token hash (or raw key); fall back to the unique alias."""
        if token:
            body: dict = {"keys": [token]}
        elif alias:
            body = {"key_aliases": [alias]}
        else:
            raise LiteLLMConfigurationError("Silinecek anahtar belirtilmedi.")
        await self._request("POST", "/key/delete", json=body)

    async def list_teams(self) -> list[dict]:
        payload = await self._request("GET", "/team/list", allow_list=True)
        teams = payload.get("teams") if isinstance(payload, dict) else payload
        return [team for team in teams or [] if isinstance(team, dict) and team.get("team_id")]

    async def add_team_member(self, team_id: str, user_id: str) -> None:
        try:
            await self._request(
                "POST",
                "/team/member_add",
                json={"team_id": team_id, "member": {"role": "user", "user_id": user_id}},
            )
        except LiteLLMConnectionError as exc:
            if "already" not in str(exc).lower():
                raise

    async def whoami(self) -> dict:
        """Describe the admin key: its owner and that owner's LiteLLM role."""
        payload = await self._request("GET", "/key/info")
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        owner = str(info.get("user_id") or "")
        role = ""
        if owner:
            user = await self.get_user(owner)
            if user:
                role = str((user.get("user_info") or {}).get("user_role") or "")
        else:
            # The proxy master key is not owned by a user and is always admin.
            role = "proxy_admin"
        return {"user_id": owner, "user_role": role}

    async def daily_activity(self, user_id: str, start: date, end: date) -> list[dict]:
        payload = await self._request(
            "GET",
            "/user/daily/activity",
            params={
                "user_id": user_id,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "page_size": 100,
            },
        )
        results = payload.get("results")
        days: list[dict] = []
        for entry in results if isinstance(results, list) else []:
            if not isinstance(entry, dict):
                continue
            metrics = entry.get("metrics") if isinstance(entry.get("metrics"), dict) else {}
            days.append(
                {
                    "date": str(entry.get("date") or ""),
                    "spend": float(metrics.get("spend") or 0),
                    "total_tokens": int(metrics.get("total_tokens") or 0),
                    "api_requests": int(metrics.get("api_requests") or 0),
                }
            )
        return sorted(days, key=lambda day: day["date"])
