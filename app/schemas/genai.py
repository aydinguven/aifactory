import re
from datetime import datetime
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator

LITELLM_USER_ROLES = {"", "internal_user", "internal_user_viewer"}
_DURATION = re.compile(r"^[1-9][0-9]{0,5}[smhd]$")


class GenAiSettingsUpdate(BaseModel):
    enabled: bool = False
    base_url: str = Field(default="", max_length=1024)
    # None keeps the stored key, "" clears it.
    admin_key: str | None = Field(default=None, max_length=4096)
    validate_tls: bool = True
    ca_cert_file: str = Field(default="", max_length=512)
    timeout_seconds: int = Field(default=15, ge=1, le=120)
    user_role: str = ""
    models: list[str] = Field(default_factory=list, max_length=100)
    max_budget: float | None = Field(default=None, ge=0)
    budget_duration: str = ""
    key_duration: str = ""
    default_team: str = Field(default="", max_length=255)
    team_priority: list[str] = Field(default_factory=list, max_length=20)
    auto_provision_directory_users: bool = True

    @field_validator(
        "base_url", "ca_cert_file", "user_role", "budget_duration", "key_duration",
        "default_team",
        mode="after",
    )
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("admin_key", mode="after")
    @classmethod
    def strip_key(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("LiteLLM URL geçerli bir http:// veya https:// adresi olmalıdır.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("LiteLLM URL kullanıcı bilgisi, query veya fragment içermemelidir.")
        path = parsed.path.rstrip("/")
        if path.endswith("/ui"):
            raise ValueError("Arayüz adresi (/ui) yerine LiteLLM kök adresini girin.")
        return value.rstrip("/")

    @field_validator("user_role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        if value not in LITELLM_USER_ROLES:
            raise ValueError("Kullanıcı rolü internal_user veya internal_user_viewer olmalıdır.")
        return value

    @field_validator("budget_duration", "key_duration")
    @classmethod
    def validate_duration(cls, value: str) -> str:
        if value and not _DURATION.fullmatch(value):
            raise ValueError("Süre 30d, 12h, 45m veya 30s biçiminde olmalıdır.")
        return value

    @field_validator("default_team", mode="after")
    @classmethod
    def validate_team(cls, value: str) -> str:
        if any(ord(ch) < 32 for ch in value):
            raise ValueError("Takım adı kontrol karakteri içeremez.")
        return value

    @field_validator("models", "team_priority", mode="after")
    @classmethod
    def validate_models(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            value = value.strip()
            if not value:
                continue
            if len(value) > 255 or any(ch.isspace() or ord(ch) < 32 for ch in value):
                raise ValueError("Model ve takım adları boşluk içeremez ve en fazla 255 karakter olabilir.")
            if value not in cleaned:
                cleaned.append(value)
        return cleaned

    @model_validator(mode="after")
    def require_url_when_enabled(self):
        if self.enabled and not self.base_url:
            raise ValueError("GenAI etkinleştirildiğinde LiteLLM URL'si zorunludur.")
        if self.ca_cert_file and not self.validate_tls:
            raise ValueError("Özel CA kullanılırken TLS doğrulaması açık olmalıdır.")
        return self


class GenAiSettingsOut(BaseModel):
    managed: bool
    enabled: bool
    base_url: str
    has_admin_key: bool
    validate_tls: bool
    ca_cert_file: str
    timeout_seconds: int
    user_role: str
    models: list[str]
    max_budget: float | None
    budget_duration: str
    key_duration: str
    default_team: str = ""
    team_priority: list[str] = Field(default_factory=list)
    auto_provision_directory_users: bool = True
    updated_at: datetime | None = None


class GenAiTestResult(BaseModel):
    ok: bool
    message: str
    admin_user_id: str = ""
    admin_role: str = ""
    latency_ms: int = 0


class GenAiUsage(BaseModel):
    spend: float = 0
    max_budget: float | None = None
    budget_duration: str | None = None
    budget_reset_at: str | None = None


class GenAiAccountStatus(BaseModel):
    configured: bool
    base_url: str = ""
    litellm_user_id: str = ""
    provisioned: bool = False
    litellm_user_exists: bool | None = None
    key_alias: str = ""
    key_active: bool | None = None
    created_at: datetime | None = None
    rotated_at: datetime | None = None
    usage: GenAiUsage | None = None
    team: str = ""
    key_team: str = ""
    key_team_current: bool | None = None
    workspace_key: bool = False
    error: str | None = None


class GenAiIssuedKey(BaseModel):
    api_key: str
    key_alias: str
    base_url: str
    litellm_user_id: str
    team: str = ""
    warning: str | None = None


class GenAiDailyUsage(BaseModel):
    date: str
    spend: float
    total_tokens: int
    api_requests: int


class GenAiUsageHistory(BaseModel):
    available: bool
    days: list[GenAiDailyUsage] = Field(default_factory=list)
