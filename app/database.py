from typing import AsyncGenerator
from sqlalchemy import event, inspect, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

# Create async engine
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    # Replace pooled connections that died (database restart, network reset)
    # instead of failing the request that happens to check them out.
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if "sqlite" in settings.DATABASE_URL else {},
)


if "sqlite" in settings.DATABASE_URL:
    @event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

# Async session factory
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for providing database sessions to FastAPI routes."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def release_read_only_connection(session: AsyncSession) -> None:
    """Finish a read-only transaction so long-running I/O does not hold a pool slot."""
    if session.in_transaction():
        await session.commit()


async def _user_column_names(conn) -> set[str]:
    return await conn.run_sync(
        lambda sync_conn: {
            column["name"] for column in inspect(sync_conn).get_columns("users")
        }
    )


async def ensure_user_quota_columns(conn) -> None:
    """Add quota columns safely, including during multi-worker startup."""
    existing_columns = await _user_column_names(conn)
    quota_columns = {
        "cpu_quota": (
            "FLOAT",
            settings.DEFAULT_USER_CPU_QUOTA,
        ),
        "memory_mb_quota": (
            "INTEGER",
            settings.DEFAULT_USER_MEMORY_MB_QUOTA,
        ),
        "disk_mb_quota": (
            "INTEGER",
            settings.DEFAULT_USER_DISK_MB_QUOTA,
        ),
        "gpu_quota": (
            "INTEGER",
            settings.DEFAULT_USER_GPU_QUOTA,
        ),
    }
    for column_name, (column_type, default_value) in quota_columns.items():
        if column_name in existing_columns:
            continue
        try:
            async with conn.begin_nested():
                await conn.execute(
                    text(
                        f"ALTER TABLE users ADD COLUMN {column_name} {column_type} "
                        f"NOT NULL DEFAULT {default_value}"
                    )
                )
        except (OperationalError, ProgrammingError):
            # Another Uvicorn worker may have completed the same migration.
            current_columns = await _user_column_names(conn)
            if column_name not in current_columns:
                raise


async def _workspace_column_names(conn) -> set[str]:
    return await conn.run_sync(
        lambda sync_conn: {
            column["name"] for column in inspect(sync_conn).get_columns("workspaces")
        }
    )


async def ensure_workspace_columns(conn) -> None:
    """Add new workspace columns safely."""
    existing_columns = await _workspace_column_names(conn)
    if "auto_stop_minutes" not in existing_columns:
        try:
            async with conn.begin_nested():
                await conn.execute(
                    text("ALTER TABLE workspaces ADD COLUMN auto_stop_minutes INTEGER NOT NULL DEFAULT 0")
                )
        except (OperationalError, ProgrammingError):
            pass
    if "node_id" not in existing_columns:
        try:
            async with conn.begin_nested():
                await conn.execute(
                    text("ALTER TABLE workspaces ADD COLUMN node_id VARCHAR(36)")
                )
        except (OperationalError, ProgrammingError):
            current_columns = await _workspace_column_names(conn)
            if "node_id" not in current_columns:
                raise
    accelerator_columns = {
        "accelerator_device_id": "VARCHAR(160)",
        "accelerator_cdi_name": "VARCHAR(200)",
        "accelerator_model": "VARCHAR(160)",
        "accelerator_kind": "VARCHAR(32)",
        "accelerator_slot": "INTEGER",
        "accelerator_memory_mb": "INTEGER NOT NULL DEFAULT 0",
        "accelerator_shared_slots": "INTEGER NOT NULL DEFAULT 0",
    }
    existing_columns = await _workspace_column_names(conn)
    for column_name, definition in accelerator_columns.items():
        if column_name in existing_columns:
            continue
        try:
            async with conn.begin_nested():
                await conn.execute(
                    text(
                        f"ALTER TABLE workspaces ADD COLUMN "
                        f"{column_name} {definition}"
                    )
                )
        except (OperationalError, ProgrammingError):
            current_columns = await _workspace_column_names(conn)
            if column_name not in current_columns:
                raise
    existing_columns = await _workspace_column_names(conn)
    if "name_key" not in existing_columns:
        # The migration backfills the values and adds the unique index; the
        # column itself is created here so a rolling restart cannot write rows
        # through the model before the ladder runs.
        try:
            async with conn.begin_nested():
                await conn.execute(
                    text(
                        "ALTER TABLE workspaces ADD COLUMN name_key "
                        "VARCHAR(100) NOT NULL DEFAULT ''"
                    )
                )
        except (OperationalError, ProgrammingError):
            current_columns = await _workspace_column_names(conn)
            if "name_key" not in current_columns:
                raise
    existing_columns = await _workspace_column_names(conn)
    if "ai_config_digest" not in existing_columns:
        # Fingerprint of the AI config a Kilo Code container was created with.
        try:
            async with conn.begin_nested():
                await conn.execute(
                    text(
                        "ALTER TABLE workspaces ADD COLUMN ai_config_digest "
                        "VARCHAR(64) NOT NULL DEFAULT ''"
                    )
                )
        except (OperationalError, ProgrammingError):
            current_columns = await _workspace_column_names(conn)
            if "ai_config_digest" not in current_columns:
                raise
    placement_columns = {
        "datacenter": "VARCHAR(40) NOT NULL DEFAULT ''",
        "tags_json": "TEXT NOT NULL DEFAULT '[]'",
    }
    existing_columns = await _workspace_column_names(conn)
    for column_name, definition in placement_columns.items():
        if column_name in existing_columns:
            continue
        try:
            async with conn.begin_nested():
                await conn.execute(
                    text(f"ALTER TABLE workspaces ADD COLUMN {column_name} {definition}")
                )
        except (OperationalError, ProgrammingError):
            current_columns = await _workspace_column_names(conn)
            if column_name not in current_columns:
                raise
    try:
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_workspaces_node_id ON workspaces (node_id)")
        )
    except (OperationalError, ProgrammingError):
        # Some managed databases use a different idempotent-index syntax; the
        # column remains functional and a real migration can add the index.
        pass


async def _download_settings_column_names(conn) -> set[str]:
    return await conn.run_sync(
        lambda sync_conn: {
            column["name"]
            for column in inspect(sync_conn).get_columns("download_settings")
        }
    )


async def ensure_download_settings_columns(conn) -> None:
    """Add HTTPS ingress settings to existing singleton settings tables."""
    existing_columns = await _download_settings_column_names(conn)
    default_hostname = settings.HTTPS_DEFAULT_HOSTNAME.replace("'", "''")
    columns = {
        "https_enabled": "BOOLEAN NOT NULL DEFAULT 0",
        "https_hostname": (
            "VARCHAR(253) NOT NULL DEFAULT "
            f"'{default_hostname}'"
        ),
        "http_fallback_enabled": "BOOLEAN NOT NULL DEFAULT 1",
        "worker_fallback_ipv4": "VARCHAR(15) NOT NULL DEFAULT ''",
        "certificate_subject": "VARCHAR(1024)",
        "certificate_not_after": "VARCHAR(64)",
        "certificate_sha256": "VARCHAR(64)",
    }
    for column_name, definition in columns.items():
        if column_name in existing_columns:
            continue
        try:
            async with conn.begin_nested():
                await conn.execute(
                    text(
                        f"ALTER TABLE download_settings ADD COLUMN "
                        f"{column_name} {definition}"
                    )
                )
        except (OperationalError, ProgrammingError):
            current_columns = await _download_settings_column_names(conn)
            if column_name not in current_columns:
                raise


async def _node_column_names(conn) -> set[str]:
    return await conn.run_sync(
        lambda sync_conn: {
            column["name"]
            for column in inspect(sync_conn).get_columns("nodes")
        }
    )


async def ensure_node_columns(conn) -> None:
    """Add telemetry and metadata columns to existing nodes tables."""
    try:
        existing_columns = await _node_column_names(conn)
    except Exception:
        return

    columns = {
        "cpu_percent": "FLOAT DEFAULT 0.0",
        "memory_used_mb": "INTEGER DEFAULT 0",
        "disk_used_mb": "INTEGER DEFAULT 0",
        "active_containers_count": "INTEGER DEFAULT 0",
        "gpu_slots_per_device": "INTEGER NOT NULL DEFAULT 0",
        "labels_json": "TEXT DEFAULT '{}'",
        "datacenter": "VARCHAR(40) NOT NULL DEFAULT ''",
        "tags_json": "TEXT NOT NULL DEFAULT '[]'",
        "capabilities_json": "TEXT DEFAULT '{}'",
        "inventory_json": "TEXT DEFAULT '[]'",
        "reconciliation_json": "TEXT DEFAULT '{}'",
        "last_reconciled_at": "TIMESTAMP",
        "agent_version": "VARCHAR(64) DEFAULT ''",
        "agent_token_hash": "VARCHAR(64) DEFAULT ''",
    }
    for column_name, definition in columns.items():
        if column_name in existing_columns:
            continue
        try:
            async with conn.begin_nested():
                await conn.execute(
                    text(f"ALTER TABLE nodes ADD COLUMN {column_name} {definition}")
                )
        except Exception:
            try:
                current_columns = await _node_column_names(conn)
                if column_name not in current_columns:
                    pass
            except Exception:
                pass


async def init_db() -> None:
    """Initialize database schemas and create tables."""
    # Ensure models are imported so Base has metadata
    from app.models.workspace_share import WorkspaceShare  # noqa: F401
    from app.models.session_settings import SessionSettings  # noqa: F401
    from app.models.onboarding import (  # noqa: F401
        OnboardingProgress,
        OnboardingSettings,
    )
    from app.models.user import User  # noqa: F401
    from app.models.user_group_quota import UserGroupQuota  # noqa: F401
    from app.models.workspace import Workspace  # noqa: F401
    from app.models.custom_template import CustomTemplate  # noqa: F401
    from app.models.directory_settings import DirectorySettings  # noqa: F401
    from app.models.directory_team import DirectoryTeam  # noqa: F401
    from app.models.node import Node  # noqa: F401
    from app.models.placement_tag import PlacementTag  # noqa: F401
    from app.models.mlflow_settings import MlflowSettings  # noqa: F401
    from app.models.mlflow_server_settings import MlflowServerSettings  # noqa: F401
    from app.models.model_container_registry_settings import (  # noqa: F401
        ModelContainerRegistrySettings,
    )
    from app.models.mlflow_deployment import (  # noqa: F401
        MlflowDeployment,
        MlflowDeploymentEvent,
        MlflowModelBuild,
    )
    from app.models.deploy_registry_settings import DeployRegistrySettings  # noqa: F401
    from app.models.bamboo_settings import BambooSettings  # noqa: F401
    from app.models.kubernetes import (  # noqa: F401
        K8sApplication,
        K8sProject,
        MlflowImageRelease,
        MlflowImageReleaseEvent,
    )
    from app.models.download_settings import DownloadSettings  # noqa: F401
    from app.models.workspace_image import WorkspaceImage  # noqa: F401
    from app.models.worker_bootstrap_ticket import WorkerBootstrapTicket  # noqa: F401
    from app.models.jupyter_ai_settings import JupyterAiSettings  # noqa: F401
    from app.models.kilo_code_settings import KiloCodeSettings  # noqa: F401
    from app.models.genai_settings import GenAiSettings  # noqa: F401
    from app.models.genai_account import GenAiAccount  # noqa: F401
    from app.models.flavor_settings import FlavorSettings  # noqa: F401
    from app.models.template_settings import TemplateSettings  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await ensure_user_quota_columns(conn)
        await ensure_workspace_columns(conn)
        await ensure_download_settings_columns(conn)
        await ensure_node_columns(conn)
