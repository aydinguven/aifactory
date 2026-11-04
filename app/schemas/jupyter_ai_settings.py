from datetime import datetime
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator


class JupyterAiModel(BaseModel):
    model_id: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=255)

    @field_validator("model_id", "name", "description", mode="after")
    @classmethod
    def validate_model_text(cls, value: str, info) -> str:
        value = value.strip()
        if any(ord(character) < 32 for character in value):
            raise ValueError("Model alanları kontrol karakteri içeremez.")
        if info.field_name == "model_id" and (
            any(character.isspace() for character in value) or "," in value
        ):
            raise ValueError("Model kimliği boşluk veya virgül içeremez.")
        return value


class JupyterAiSettingsUpdate(BaseModel):
    enabled: bool = False
    cline_enabled: bool | None = None
    gateway_url: str = Field(default="", max_length=512)
    model_id: str = Field(default="", max_length=255)
    gateway_model_discovery: bool = False
    models: list[JupyterAiModel] | None = None
    shared_token: str | None = Field(default=None, max_length=4096)
    # None keeps the stored bundle, "" removes it.
    ca_bundle_pem: str | None = Field(default=None, max_length=300_000)

    @field_validator("gateway_url", "model_id", mode="after")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("gateway_url")
    @classmethod
    def validate_gateway_url(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(
                "Gateway URL http:// veya https:// ile başlayan geçerli bir adres olmalıdır."
            )
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "Gateway URL kullanıcı bilgisi, query veya fragment içermemelidir."
            )
        return value.rstrip("/")

    @field_validator("model_id")
    @classmethod
    def validate_model_id(cls, value: str) -> str:
        if any(character.isspace() for character in value):
            raise ValueError("Model kimliği boşluk içeremez.")
        return value

    @model_validator(mode="after")
    def require_enabled_fields(self):
        if self.enabled and (not self.gateway_url or not self.model_id):
            raise ValueError(
                "Workspace AI etkinleştirildiğinde gateway URL ve model kimliği zorunludur."
            )
        if self.models is not None:
            if len(self.models) > 50:
                raise ValueError("En fazla 50 sabit model tanımlanabilir.")
            model_ids = [model.model_id for model in self.models]
            if len(model_ids) != len(set(model_ids)):
                raise ValueError("Model kimlikleri benzersiz olmalıdır.")
            if self.enabled and self.model_id not in model_ids:
                raise ValueError("Varsayılan model model kataloğunda bulunmalıdır.")
        return self


class JupyterAiSettingsOut(BaseModel):
    managed: bool
    enabled: bool
    cline_enabled: bool
    gateway_url: str
    model_id: str
    gateway_model_discovery: bool
    models: list[JupyterAiModel]
    has_shared_token: bool
    ca_bundle_pem: str = ""
    updated_at: datetime | None = None


class JupyterAiConnectivityTestRequest(BaseModel):
    model_id: str = Field(min_length=1, max_length=255)

    @field_validator("model_id", mode="after")
    @classmethod
    def validate_model_id(cls, value: str) -> str:
        value = value.strip()
        if any(character.isspace() for character in value):
            raise ValueError("Model kimligi bosluk iceremez.")
        return value


class JupyterAiConnectivityTargetResult(BaseModel):
    node_id: str
    node_name: str
    ok: bool
    model_id: str
    status_code: int | None = None
    latency_ms: int | None = None
    message: str


class JupyterAiConnectivityTestResult(BaseModel):
    ok: bool
    model_id: str
    workers: list[JupyterAiConnectivityTargetResult]


class WorkerJupyterAiSettings(BaseModel):
    managed: bool
    enabled: bool
    cline_enabled: bool = False
    gateway_url: str = ""
    model_id: str = ""
    gateway_model_discovery: bool = False
    models: list[JupyterAiModel] = Field(default_factory=list)
    shared_token: str = ""
    updated_at: datetime | None = None
