import ipaddress
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.jupyter_ai import normalize_ca_bundle

_HOST_LABEL = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


def normalize_collector_host(value: str) -> str:
    """A hostname or IP address without scheme, port or path."""
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if not value:
        return ""
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    labels = value.rstrip(".").split(".")
    if len(value) > 253 or not all(_HOST_LABEL.fullmatch(label) for label in labels):
        raise ValueError(
            "Toplayıcı adresi yalnızca host adı veya IP olmalıdır; şema, port veya yol eklemeyin."
        )
    return value


class LogForwardingFields(BaseModel):
    forward_enabled: bool = False
    forward_host: str = Field(default="", max_length=255)
    forward_port: int = Field(default=514, ge=1, le=65535)
    forward_protocol: Literal["udp", "tcp", "tls"] = "tcp"
    forward_format: Literal["leef", "rfc5424"] = "leef"
    forward_tls_verify: bool = True
    forward_ca_pem: str = Field(default="", max_length=300_000)
    forward_audit: bool = True
    forward_app: bool = True
    forward_app_min_level: LogLevel = "WARNING"
    forward_access: bool = False

    @field_validator("forward_host", mode="after")
    @classmethod
    def validate_host(cls, value: str) -> str:
        return normalize_collector_host(value)

    @field_validator("forward_ca_pem", mode="after")
    @classmethod
    def validate_ca(cls, value: str) -> str:
        if not value.strip():
            return ""
        bundle = normalize_ca_bundle(value)
        if not bundle:
            raise ValueError("CA paketi yalnızca PEM sertifika blokları içermelidir.")
        return bundle

    @model_validator(mode="after")
    def require_host(self):
        if self.forward_enabled and not self.forward_host:
            raise ValueError("Yönlendirme etkinken toplayıcı adresi zorunludur.")
        return self


class LoggingSettingsUpdate(LogForwardingFields):
    retention_days: int = Field(default=14, ge=1, le=365)
    max_total_mb: int = Field(default=1024, ge=50, le=102_400)
    log_level: LogLevel = "INFO"


class LoggingSettingsOut(LoggingSettingsUpdate):
    managed: bool
    revision: int
    updated_at: datetime | None = None
    updated_by: str = ""


class WorkerLoggingSettings(LoggingSettingsUpdate):
    """Policy an enrolled worker fetches; ``managed=False`` keeps env defaults."""

    managed: bool
    revision: int = 0


class LogForwardingTestRequest(LogForwardingFields):
    forward_enabled: bool = True


class LogForwardingTestResult(BaseModel):
    ok: bool
    message: str
    latency_ms: int | None = None
