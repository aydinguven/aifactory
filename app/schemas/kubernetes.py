import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

RFS_CODE_PATTERN = re.compile(r"^[a-z0-9]{2,32}$")
NAMESPACE_PATTERN = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
APP_NAME_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
RELEASE_VERSION_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def _no_control_characters(value: str, label: str) -> str:
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{label} kontrol karakteri içeremez.")
    return value


def normalize_deploy_registry_host(value: str) -> str:
    """A registry host[:port] without scheme or path; images go under rfs/app."""
    value = value.strip().rstrip("/")
    if not value:
        return ""
    if "://" in value:
        raise ValueError("Registry adresi http:// veya https:// içermemelidir; host:port kullanın.")
    if any(ord(character) < 33 for character in value) or any(
        character in value for character in "@?#/"
    ):
        raise ValueError(
            "Registry adresi yalnızca host:port olmalıdır; yol, kimlik bilgisi veya query içeremez."
        )
    host, _, port = value.partition(":")
    if not host or host in {".", ".."} or (port and not port.isdigit()) or value.endswith(":"):
        raise ValueError("Geçerli bir registry host:port adresi girin.")
    return value


def _username(value: str) -> str:
    return _no_control_characters(value.strip(), "Registry kullanıcı adı")


def _app_name(value: str) -> str:
    value = value.lower()
    if not APP_NAME_PATTERN.fullmatch(value):
        raise ValueError(
            "Uygulama adı küçük harf, rakam ve tire içermelidir (örn. mbnllmdml-swiftanomaly)."
        )
    return value


class DeployRegistrySettingsUpdate(BaseModel):
    enabled: bool = False
    registry_url: str = Field(default="", max_length=255)
    username: str = Field(default="", max_length=255)
    password: str | None = Field(default=None, max_length=4096)
    tls_verify: bool = True
    manifest_format: Literal["", "v2s2"] = ""

    @field_validator("registry_url", mode="after")
    @classmethod
    def validate_registry_url(cls, value: str) -> str:
        return normalize_deploy_registry_host(value)

    @field_validator("username", mode="after")
    @classmethod
    def validate_username(cls, value: str) -> str:
        return _username(value)


class DeployRegistrySettingsOut(BaseModel):
    managed: bool
    enabled: bool
    registry_url: str
    username: str
    has_password: bool
    tls_verify: bool
    manifest_format: str
    updated_at: datetime | None = None


class DeployRegistryTestRequest(BaseModel):
    registry_url: str = Field(min_length=1, max_length=255)
    username: str = Field(default="", max_length=255)
    password: str | None = Field(default=None, max_length=4096)
    tls_verify: bool = True

    @field_validator("registry_url", mode="after")
    @classmethod
    def validate_registry_url(cls, value: str) -> str:
        normalized = normalize_deploy_registry_host(value)
        if not normalized:
            raise ValueError("Registry adresi zorunludur.")
        return normalized

    @field_validator("username", mode="after")
    @classmethod
    def validate_username(cls, value: str) -> str:
        return _username(value)


class DeployRegistryTestResult(BaseModel):
    ok: bool
    registry_url: str
    status_code: int | None = None
    latency_ms: int | None = None
    message: str


BAMBOO_PLAN_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9]*-[A-Z][A-Z0-9]*$")


def normalize_bamboo_base_url(value: str) -> str:
    """The Bamboo root URL, e.g. https://atlas.example/bamboo, without a trailing slash."""
    value = value.strip().rstrip("/")
    if not value:
        return ""
    if not re.fullmatch(r"https?://[^\s/?#@]+(/[^\s?#]*)?", value):
        raise ValueError(
            "Bamboo adresi http:// veya https:// ile başlamalı; kimlik bilgisi veya query içermemelidir."
        )
    return value


def _plan_key(value: str) -> str:
    value = value.strip().upper()
    if not BAMBOO_PLAN_KEY_PATTERN.fullmatch(value):
        raise ValueError("Plan anahtarı PROJE-PLAN biçiminde olmalıdır (örn. AIMLOPS-RETAG).")
    return value


class BambooSettingsUpdate(BaseModel):
    enabled: bool = False
    base_url: str = Field(default="", max_length=255)
    plan_key: str = Field(default="AIMLOPS-RETAG", max_length=64)
    token: str | None = Field(default=None, max_length=4096)
    tls_verify: bool = True

    @field_validator("base_url", mode="after")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return normalize_bamboo_base_url(value)

    @field_validator("plan_key", mode="after")
    @classmethod
    def validate_plan_key(cls, value: str) -> str:
        return _plan_key(value)

    @field_validator("token", mode="after")
    @classmethod
    def validate_token(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if any(ord(character) < 33 for character in value):
            raise ValueError("Bamboo tokenı boşluk veya kontrol karakteri içeremez.")
        return value


class BambooSettingsOut(BaseModel):
    managed: bool
    enabled: bool
    base_url: str
    plan_key: str
    has_token: bool
    tls_verify: bool
    updated_at: datetime | None = None


class K8sProjectWrite(BaseModel):
    rfs_code: str = Field(min_length=2, max_length=32)
    name: str = Field(min_length=1, max_length=120)
    # Empty means the RFS code, which is always a valid DNS label.
    namespace: str = Field(default="", max_length=63)
    description: str = Field(default="", max_length=2000)
    enabled: bool = True

    model_config = {"str_strip_whitespace": True}

    @model_validator(mode="after")
    def default_namespace(self):
        if not self.namespace:
            self.namespace = self.rfs_code
        return self

    @field_validator("rfs_code", mode="after")
    @classmethod
    def validate_rfs_code(cls, value: str) -> str:
        value = value.lower()
        if not RFS_CODE_PATTERN.fullmatch(value):
            raise ValueError("RFS kodu 2-32 küçük harf veya rakam olmalıdır (örn. mbnllmd).")
        return value

    @field_validator("namespace", mode="after")
    @classmethod
    def validate_namespace(cls, value: str) -> str:
        value = value.lower()
        if value and not NAMESPACE_PATTERN.fullmatch(value):
            raise ValueError(
                "Namespace küçük harf, rakam ve tire içeren geçerli bir DNS etiketi olmalıdır."
            )
        return value

    @field_validator("name", "description", mode="after")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return value.replace("\r\n", "\n")


class K8sApplicationWrite(BaseModel):
    app_name: str = Field(min_length=1, max_length=100)
    mlflow_model_name: str = Field(min_length=1, max_length=255)
    bitbucket_url: str = Field(default="", max_length=500)
    description: str = Field(default="", max_length=2000)
    enabled: bool = True

    model_config = {"str_strip_whitespace": True}

    @field_validator("app_name", mode="after")
    @classmethod
    def validate_app_name(cls, value: str) -> str:
        return _app_name(value)

    @field_validator("mlflow_model_name", mode="after")
    @classmethod
    def validate_model_name(cls, value: str) -> str:
        return _no_control_characters(value, "MLflow model adı")

    @field_validator("bitbucket_url", mode="after")
    @classmethod
    def validate_bitbucket_url(cls, value: str) -> str:
        if value and not re.match(r"^https?://[^\s]+$", value):
            raise ValueError("Bitbucket adresi http:// veya https:// ile başlamalıdır.")
        return value


class K8sApplicationOut(BaseModel):
    id: str
    project_id: str
    app_name: str
    repository: str
    mlflow_model_name: str
    bitbucket_url: str
    description: str
    enabled: bool
    release_count: int = 0
    last_version: str | None = None
    locked: bool = False


class K8sProjectOut(BaseModel):
    id: str
    rfs_code: str
    name: str
    namespace: str
    description: str
    enabled: bool
    locked: bool = False
    applications: list[K8sApplicationOut] = []


class K8sProjectList(BaseModel):
    projects: list[K8sProjectOut]


class KubernetesProjectOption(BaseModel):
    id: str
    rfs_code: str
    name: str
    namespace: str


class KubernetesApplicationOption(BaseModel):
    """An application already bound to the selected model."""

    id: str
    project_id: str
    app_name: str
    repository: str
    last_version: str | None = None
    suggested_version: str


class KubernetesOptions(BaseModel):
    enabled: bool
    can_publish: bool
    registry_url: str
    message: str = ""
    projects: list[KubernetesProjectOption] = []
    applications: list[KubernetesApplicationOption] = []


class ImageReleaseCreate(BaseModel):
    """The application is created on its first release and bound to the model."""

    project_id: str = Field(min_length=1, max_length=36)
    app_name: str = Field(min_length=1, max_length=100)
    model_name: str = Field(min_length=1, max_length=255)
    model_version: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=5, max_length=32)

    model_config = {"str_strip_whitespace": True}

    @field_validator("app_name", mode="after")
    @classmethod
    def validate_app_name(cls, value: str) -> str:
        return _app_name(value)

    @field_validator("model_version")
    @classmethod
    def immutable_numeric_version(cls, value: str) -> str:
        if not value.isdigit() or int(value) < 1:
            raise ValueError("Yayın için pozitif, sayısal bir model versiyonu gereklidir.")
        return str(int(value))

    @field_validator("version")
    @classmethod
    def semantic_release_version(cls, value: str) -> str:
        if not RELEASE_VERSION_PATTERN.fullmatch(value):
            raise ValueError(
                "Versiyon X.Y.Z biçiminde olmalıdır (örn. 1.0.0); RC ve build numarasını platform ekibi ekler."
            )
        return value


class ImageReleaseOut(BaseModel):
    id: str
    user_id: int
    username: str = ""
    application_id: str
    app_name: str = ""
    project_name: str = ""
    rfs_code: str = ""
    namespace: str = ""
    model_name: str
    model_version: str
    run_id: str
    version: str
    image_ref: str
    digest: str
    mlflow_tagged: bool
    status: str
    status_message: str
    error_message: str | None = None
    build_status: str | None = None
    build_error_message: str | None = None
    bamboo_status: str = ""
    bamboo_result_key: str = ""
    bamboo_result_url: str = ""
    bamboo_error: str | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class ImageReleaseList(BaseModel):
    releases: list[ImageReleaseOut]


class ImageReleaseEventOut(BaseModel):
    sequence: int
    level: str
    message: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ImageReleaseEvents(BaseModel):
    events: list[ImageReleaseEventOut]
