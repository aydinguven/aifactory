from pathlib import Path
from typing import ClassVar

from pydantic_settings import BaseSettings, SettingsConfigDict
from app import __version__


class Settings(BaseSettings):
    """Application settings and configuration."""
    
    APP_NAME: str = "AI Factory Çalışma Alanı Yönetimi"
    APP_VERSION: ClassVar[str] = __version__
    ENV: str = "development"
    DEBUG: bool = True
    
    # Security
    SECRET_KEY: str = "aifactory-super-secret-key-change-in-production-1234567890"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 4  # Initial value; admin policy takes precedence.
    # Sessions set before 3.50.1 use devcloud_session; it is still read
    # (app.auth.dependencies.session_cookie_names) and never forwarded.
    COOKIE_NAME: str = "aifactory_session"
    COOKIE_SECURE: bool = False
    
    # Storage & Database
    BASE_DIR: Path = Path(__file__).resolve().parent.parent
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/aifactory.db"
    STORAGE_ROOT: str = str(Path(__file__).resolve().parent.parent / "data" / "workspaces")
    AUTO_MIGRATE: bool = True

    # Deployment topology. All-in-one is a controller plus an ordinary worker
    # agent on the same host; the controller never runs workspace containers.
    AIFACTORY_DEPLOYMENT_ROLE: str = "controller"
    AIFACTORY_BOOTSTRAP_WORKER_ID: str = ""
    AIFACTORY_BOOTSTRAP_WORKER_NAME: str = ""
    AIFACTORY_BOOTSTRAP_WORKER_TOKEN_HASH: str = ""
    AIFACTORY_REGISTRY_MODE: str = "preloaded"
    AIFACTORY_REGISTRY_URL: str = ""
    AIFACTORY_REGISTRY_USERNAME: str = ""
    AIFACTORY_REGISTRY_PASSWORD: str = ""
    # Preferred bootstrap fallback for generated ML model serving images.
    # Admin-managed encrypted settings take precedence once saved.
    MODEL_CONTAINER_REGISTRY_URL: str = ""
    MODEL_CONTAINER_REGISTRY_USERNAME: str = ""
    MODEL_CONTAINER_REGISTRY_PASSWORD: str = ""
    MLFLOW_BIN: str = "mlflow"
    MLFLOW_MODEL_BUILD_TIMEOUT_SECONDS: int = 1800
    
    # Quotas assigned to newly registered users (admins can override each user).
    DEFAULT_USER_CPU_QUOTA: float = 1.0
    DEFAULT_USER_MEMORY_MB_QUOTA: int = 1024
    DEFAULT_USER_DISK_MB_QUOTA: int = 10240
    DEFAULT_USER_GPU_QUOTA: int = 0

    # Offline bundle publishing (admin-only; disable updates on disconnected hosts).
    DOWNLOADS_ENABLED: bool = True
    DOWNLOAD_UPDATES_ENABLED: bool = True
    DOWNLOADS_ROOT: str = "/srv/aifactory-downloads"
    DOWNLOAD_BUILD_ROOT: str = str(BASE_DIR / "data" / "download-builds")
    DOWNLOAD_TARGET_PYTHON_VERSION: str = ""
    DOWNLOAD_PUBLIC_BASE_URL: str = "http://10.253.6.189"
    UPDATES_ENABLED: bool = True
    UPDATE_SOURCE_TYPE: str = "bundle"
    UPDATE_SOURCE: str = ""
    UPDATE_REF: str = "stable"
    UPDATE_QUEUE_ROOT: str = "/var/lib/aifactory/update-queue"
    # Bulk AD sync shortly after startup and then every N hours; 0 disables.
    DIRECTORY_SYNC_INTERVAL_HOURS: float = 6
    UPDATE_MAX_UPLOAD_BYTES: int = 8 * 1024 * 1024 * 1024
    # Temporary, deployment-scoped compatibility switch. Keep disabled unless
    # the operator explicitly accepts checksum-only worker OTA verification.
    WORKER_OTA_ALLOW_UNSIGNED: bool = False
    WORKSPACE_IMAGES_ROOT: str = "/srv/aifactory-downloads/workspace-images"
    WORKSPACE_IMAGE_MAX_UPLOAD_BYTES: int = 16 * 1024 * 1024 * 1024
    WORKSPACE_IMAGE_IMPORT_TIMEOUT_SECONDS: int = 1800
    SKOPEO_BIN: str = "skopeo"

    # Nginx ingress is applied by a root-owned, narrowly scoped helper.
    INGRESS_STAGING_ROOT: str = "/var/lib/aifactory/ingress"
    INGRESS_APPLY_COMMAND: str = "/usr/local/libexec/aifactory-apply-ingress"
    HTTPS_DEFAULT_HOSTNAME: str = "127.0.0.1"

    # Podman Configuration
    PODMAN_BIN: str = "podman"
    PODMAN_NETWORK: str = "bridge"
    USE_MOCK_PODMAN: bool = False
    PODMAN_RUN_TIMEOUT_SECONDS: float = 30.0

    # Optional on-prem gateway defaults for managed workspace AI clients. The
    # shared credential is intentionally forwarded to every AI-enabled
    # workspace hosted by the worker.
    JUPYTER_AI_GATEWAY_URL: str = ""
    JUPYTER_AI_MODEL: str = ""
    JUPYTER_AI_GATEWAY_TOKEN: str = ""
    JUPYTER_AI_GATEWAY_MODEL_DISCOVERY: bool = False
    JUPYTER_AI_MODEL_CATALOG_JSON: str = "[]"
    JUPYTER_AI_CLINE_ENABLED: bool = True
    
    # Files are acknowledged in 256 KiB chunks; these are whole-transfer limits.
    FILE_TRANSFER_MAX_BYTES: int = 8 * 1024 * 1024 * 1024
    PROXY_MAX_REQUEST_BYTES: int = 512 * 1024 * 1024
    WORKER_MAX_TRANSFERS: int = 32

    # Container Port Range for internal forwarding
    PORT_RANGE_START: int = 10100
    PORT_RANGE_END: int = 12000
    
    # Initial Admin Seed
    ADMIN_USERNAME: str = "admin"
    ADMIN_EMAIL: str = "admin@example.com"
    ADMIN_PASSWORD: str = "admin123"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
