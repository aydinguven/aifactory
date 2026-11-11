"""Small, explicit, database-portable schema migration runner.

The project historically altered tables during web-process startup.  Managed
installations now run this module before restarting the controller, which
keeps schema ownership out of Uvicorn and works with SQLite or PostgreSQL.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import inspect, text

from app.config import settings
from app.database import engine, init_db


CURRENT_SCHEMA_VERSION = 40
# ai_config_digest of the workspaces migration 33 moved off TCMB Code.
TCMB_CODE_MIGRATED_DIGEST = "tcmb-code-migrated"


class MigrationError(RuntimeError):
    pass


def _rebuild_sqlite_workspaces(sync_conn) -> bool:
    """Replace a legacy SQLite workspace table with the worker-only schema."""
    from app.models.workspace import Workspace

    inspector = inspect(sync_conn)
    columns = inspector.get_columns("workspaces")
    node_column = next(column for column in columns if column["name"] == "node_id")
    unique_sets = {
        tuple(constraint.get("column_names") or ())
        for constraint in inspector.get_unique_constraints("workspaces")
    }
    unique_sets.update(
        tuple(index.get("column_names") or ())
        for index in inspector.get_indexes("workspaces")
        if index.get("unique")
    )
    if not node_column.get("nullable", True) and ("host_port",) not in unique_sets:
        return False

    legacy = "workspaces_legacy_v3"
    if legacy in inspector.get_table_names():
        raise MigrationError(
            f"Cannot rebuild workspaces while stale table {legacy!r} exists"
        )
    preparer = sync_conn.dialect.identifier_preparer
    sync_conn.exec_driver_sql(
        f"ALTER TABLE {preparer.quote('workspaces')} "
        f"RENAME TO {preparer.quote(legacy)}"
    )
    legacy_inspector = inspect(sync_conn)
    for index in legacy_inspector.get_indexes(legacy):
        name = str(index.get("name") or "")
        if name and not name.startswith("sqlite_autoindex"):
            sync_conn.exec_driver_sql(
                f"DROP INDEX {preparer.quote(name)}"
            )
    Workspace.__table__.create(sync_conn)
    old_columns = {
        column["name"] for column in legacy_inspector.get_columns(legacy)
    }
    common = [
        column.name for column in Workspace.__table__.columns
        if column.name in old_columns
    ]
    quoted = ", ".join(preparer.quote(name) for name in common)
    sync_conn.exec_driver_sql(
        f"INSERT INTO {preparer.quote('workspaces')} ({quoted}) "
        f"SELECT {quoted} FROM {preparer.quote(legacy)}"
    )
    sync_conn.exec_driver_sql(f"DROP TABLE {preparer.quote(legacy)}")
    return True


# Releases before 3.50.1 use the devcloud table name; upgrade() renames it.
# Both names are read, so 3.49.x (which also reads both) can still roll back.
MIGRATIONS_TABLE = "aifactory_schema_migrations"
LEGACY_MIGRATIONS_TABLE = "devcloud_schema_migrations"


async def migrations_table(conn) -> str | None:
    tables = await conn.run_sync(
        lambda sync_conn: set(inspect(sync_conn).get_table_names())
    )
    for name in (MIGRATIONS_TABLE, LEGACY_MIGRATIONS_TABLE):
        if name in tables:
            return name
    return None


async def _record_version(conn, version: int, name: str) -> None:
    table = await migrations_table(conn) or MIGRATIONS_TABLE
    await conn.execute(
        text(
            f"INSERT INTO {table} "
            "(version, name, applied_at) VALUES (:version, :name, :applied_at)"
        ),
        {
            "version": version,
            "name": name,
            "applied_at": datetime.now(timezone.utc).isoformat(),
        },
    )


async def _expand_flavor_settings(conn) -> None:
    columns = await conn.run_sync(
        lambda sync_conn: {
            column["name"]
            for column in inspect(sync_conn).get_columns("flavor_settings")
        }
    )
    definitions = {
        "is_custom": "BOOLEAN NOT NULL DEFAULT FALSE",
        "display_name": "VARCHAR(100) NOT NULL DEFAULT ''",
        "description": "VARCHAR(255) NOT NULL DEFAULT ''",
        "cpus": "FLOAT",
        "memory_mb": "INTEGER",
        "accelerator_count": "INTEGER",
        "accelerator_vendor": "VARCHAR(50) NOT NULL DEFAULT ''",
        "accelerator_memory_mb": "INTEGER",
    }
    for name, definition in definitions.items():
        if name not in columns:
            await conn.execute(text(f"ALTER TABLE flavor_settings ADD COLUMN {name} {definition}"))


async def _applied_versions(conn) -> set[int]:
    table = await migrations_table(conn)
    if table is None:
        table = MIGRATIONS_TABLE
        await conn.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {table} ("
                "version INTEGER PRIMARY KEY, "
                "name VARCHAR(255) NOT NULL, "
                "applied_at VARCHAR(64) NOT NULL)"
            )
        )
    rows = await conn.execute(text(f"SELECT version FROM {table}"))
    return {int(row[0]) for row in rows}


async def _seed_bootstrap_worker(conn) -> None:
    worker_id = settings.AIFACTORY_BOOTSTRAP_WORKER_ID.strip()
    token_hash = settings.AIFACTORY_BOOTSTRAP_WORKER_TOKEN_HASH.strip()
    if not worker_id:
        return
    if len(token_hash) != hashlib.sha256().digest_size * 2:
        raise MigrationError("Bootstrap worker token hash is invalid")
    worker_name = (
        settings.AIFACTORY_BOOTSTRAP_WORKER_NAME.strip() or "all-in-one-worker"
    )
    exists = await conn.execute(
        text("SELECT id FROM nodes WHERE id = :id"), {"id": worker_id}
    )
    if exists.first():
        await conn.execute(
            text(
                "UPDATE nodes SET name = :name, agent_token_hash = :token_hash, "
                "enabled = :enabled, schedulable = :schedulable WHERE id = :id"
            ),
            {
                "id": worker_id,
                "name": worker_name,
                "token_hash": token_hash,
                "enabled": True,
                "schedulable": True,
            },
        )
        return
    await conn.execute(
        text(
            "INSERT INTO nodes "
            "(id, name, hostname, enabled, schedulable, status, cpu_total, "
            "memory_total_mb, disk_total_mb, cpu_percent, memory_used_mb, "
            "disk_used_mb, active_containers_count, labels_json, capabilities_json, "
            "inventory_json, reconciliation_json, agent_version, agent_token_hash, "
            "created_at, updated_at) "
            "VALUES (:id, :name, '', :enabled, :schedulable, :status, 0, 0, 0, "
            "0, 0, 0, 0, '{}', '{}', '[]', '{}', '', :token_hash, :created_at, "
            ":updated_at)"
        ),
        {
            "id": worker_id,
            "name": worker_name,
            "enabled": True,
            "schedulable": True,
            "status": "PENDING",
            "token_hash": token_hash,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        },
    )


async def _assign_legacy_workspaces(conn) -> None:
    count = int(
        (
            await conn.execute(
                text("SELECT COUNT(*) FROM workspaces WHERE node_id IS NULL")
            )
        ).scalar_one()
    )
    if not count:
        return
    rows = (
        await conn.execute(text("SELECT id FROM nodes ORDER BY created_at, id"))
    ).all()
    if len(rows) != 1:
        raise MigrationError(
            f"{count} legacy workspace(s) have no worker assignment. "
            "Register exactly one migration target worker, or assign node_id "
            "before upgrading."
        )
    await conn.execute(
        text("UPDATE workspaces SET node_id = :node_id WHERE node_id IS NULL"),
        {"node_id": rows[0][0]},
    )


async def _worker_only_constraints(conn) -> None:
    await _seed_bootstrap_worker(conn)
    await _assign_legacy_workspaces(conn)
    dialect = conn.dialect.name
    if dialect == "postgresql":
        constraints = await conn.run_sync(
            lambda sync_conn: inspect(sync_conn).get_unique_constraints(
                "workspaces"
            )
        )
        preparer = conn.dialect.identifier_preparer
        for constraint in constraints:
            if constraint.get("column_names") == ["host_port"] and constraint.get(
                "name"
            ):
                quoted = preparer.quote(constraint["name"])
                await conn.execute(
                    text(f"ALTER TABLE workspaces DROP CONSTRAINT {quoted}")
                )
        await conn.execute(
            text("ALTER TABLE workspaces ALTER COLUMN node_id SET NOT NULL")
        )
    elif dialect == "sqlite":
        await conn.run_sync(_rebuild_sqlite_workspaces)
    unique_sets = await conn.run_sync(
        lambda sync_conn: {
            tuple(item.get("column_names") or ())
            for item in (
                inspect(sync_conn).get_unique_constraints("workspaces")
                + [
                    index
                    for index in inspect(sync_conn).get_indexes("workspaces")
                    if index.get("unique")
                ]
            )
        }
    )
    if ("node_id", "host_port") not in unique_sets:
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_workspaces_node_host_port "
                "ON workspaces (node_id, host_port)"
            )
        )


async def _make_mlflow_settings_per_user(conn) -> None:
    """Add user ownership without assigning the legacy shared credential."""
    columns = await conn.run_sync(
        lambda sync_conn: {
            column["name"]
            for column in inspect(sync_conn).get_columns("mlflow_settings")
        }
    )
    if "user_id" not in columns:
        await conn.execute(
            text("ALTER TABLE mlflow_settings ADD COLUMN user_id INTEGER")
        )
    unique_sets = await conn.run_sync(
        lambda sync_conn: {
            tuple(item.get("column_names") or ())
            for item in (
                inspect(sync_conn).get_unique_constraints("mlflow_settings")
                + [
                    index
                    for index in inspect(sync_conn).get_indexes("mlflow_settings")
                    if index.get("unique")
                ]
            )
        }
    )
    if ("user_id",) not in unique_sets:
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX uq_mlflow_settings_user_id "
                "ON mlflow_settings (user_id)"
            )
        )


async def _sync_mlflow_settings_id_sequence(conn) -> None:
    """Create/advance the PostgreSQL sequence missing from the legacy table.

    The original singleton MLflow configuration always inserted ``id=1``.
    Its Python-side default also caused SQLAlchemy to create the PostgreSQL ID
    column without a SERIAL default. The first per-user insert therefore had no
    ID to use. SQLite allocates ``MAX(id) + 1`` itself and needs no repair.
    """
    if conn.dialect.name != "postgresql":
        return
    await conn.execute(
        text("CREATE SEQUENCE IF NOT EXISTS mlflow_settings_id_seq")
    )
    await conn.execute(
        text(
            "ALTER SEQUENCE mlflow_settings_id_seq "
            "OWNED BY mlflow_settings.id"
        )
    )
    await conn.execute(
        text(
            "ALTER TABLE mlflow_settings ALTER COLUMN id "
            "SET DEFAULT nextval('mlflow_settings_id_seq')"
        )
    )
    await conn.execute(
        text(
            "SELECT setval("
            "'mlflow_settings_id_seq', "
            "COALESCE(MAX(id), 1), "
            "MAX(id) IS NOT NULL"
            ") FROM mlflow_settings"
        )
    )


async def _add_directory_profile_fields(conn) -> None:
    """Add LDAP-backed organization fields to legacy databases idempotently."""
    user_columns = await conn.run_sync(
        lambda sync_conn: {
            column["name"] for column in inspect(sync_conn).get_columns("users")
        }
    )
    if "team" not in user_columns:
        await conn.execute(
            text("ALTER TABLE users ADD COLUMN team VARCHAR(255) NOT NULL DEFAULT ''")
        )
    if "directorate" not in user_columns:
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN directorate "
                "VARCHAR(255) NOT NULL DEFAULT ''"
            )
        )

    directory_columns = await conn.run_sync(
        lambda sync_conn: {
            column["name"]
            for column in inspect(sync_conn).get_columns("directory_settings")
        }
    )
    if "team_attribute" not in directory_columns:
        await conn.execute(
            text(
                "ALTER TABLE directory_settings ADD COLUMN team_attribute "
                "VARCHAR(128) NOT NULL DEFAULT 'department'"
            )
        )
    if "directorate_attribute" not in directory_columns:
        await conn.execute(
            text(
                "ALTER TABLE directory_settings ADD COLUMN directorate_attribute "
                "VARCHAR(128) NOT NULL DEFAULT 'division'"
            )
        )


async def _add_directory_hierarchy_fields(conn) -> None:
    """Add an optional intermediate LDAP organization level idempotently."""
    user_columns = await conn.run_sync(
        lambda sync_conn: {
            column["name"] for column in inspect(sync_conn).get_columns("users")
        }
    )
    if "organization_unit" not in user_columns:
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN organization_unit "
                "VARCHAR(255) NOT NULL DEFAULT ''"
            )
        )

    directory_columns = await conn.run_sync(
        lambda sync_conn: {
            column["name"]
            for column in inspect(sync_conn).get_columns("directory_settings")
        }
    )
    if "organization_unit_attribute" not in directory_columns:
        await conn.execute(
            text(
                "ALTER TABLE directory_settings "
                "ADD COLUMN organization_unit_attribute "
                "VARCHAR(128) NOT NULL DEFAULT ''"
            )
        )


_QUOTA_OVERRIDE_COLUMNS = {
    "cpu_quota": "FLOAT",
    "memory_mb_quota": "INTEGER",
    "disk_mb_quota": "INTEGER",
    "gpu_quota": "INTEGER",
}


async def _add_team_quota_groups(conn) -> None:
    """Introduce override -> team group -> default quota resolution.

    Every user keeps exactly the quota they had: a legacy value that differs
    from the configured default becomes an explicit override, and the current
    defaults seed the editable default group.
    """
    user_columns = await conn.run_sync(
        lambda sync_conn: {
            column["name"] for column in inspect(sync_conn).get_columns("users")
        }
    )
    for name, column_type in _QUOTA_OVERRIDE_COLUMNS.items():
        if f"{name}_override" not in user_columns:
            await conn.execute(
                text(f"ALTER TABLE users ADD COLUMN {name}_override {column_type}")
            )

    defaults = {
        "cpu_quota": float(settings.DEFAULT_USER_CPU_QUOTA),
        "memory_mb_quota": int(settings.DEFAULT_USER_MEMORY_MB_QUOTA),
        "disk_mb_quota": int(settings.DEFAULT_USER_DISK_MB_QUOTA),
        "gpu_quota": int(settings.DEFAULT_USER_GPU_QUOTA),
    }
    existing_default = (
        await conn.execute(
            text("SELECT id FROM user_group_quotas WHERE group_key = ''")
        )
    ).first()
    if existing_default is None:
        now = datetime.now(timezone.utc)
        await conn.execute(
            text(
                "INSERT INTO user_group_quotas (group_key, display_name, "
                "cpu_quota, memory_mb_quota, disk_mb_quota, gpu_quota, "
                "created_at, updated_at) VALUES ('', 'Varsayılan', :cpu, :mem, "
                ":disk, :gpu, :now, :now)"
            ),
            {
                "cpu": defaults["cpu_quota"],
                "mem": defaults["memory_mb_quota"],
                "disk": defaults["disk_mb_quota"],
                "gpu": defaults["gpu_quota"],
                "now": now,
            },
        )

    legacy = [name for name in _QUOTA_OVERRIDE_COLUMNS if name in user_columns]
    for name in legacy:
        await conn.execute(
            text(
                f"UPDATE users SET {name}_override = {name} "
                f"WHERE {name}_override IS NULL AND {name} IS NOT NULL "
                f"AND {name} <> :default"
            ),
            {"default": defaults[name]},
        )


async def _add_gpu_allocation_constraints(conn) -> None:
    """Protect one workspace reservation per worker GPU device slot."""
    unique_sets = await conn.run_sync(
        lambda sync_conn: {
            tuple(item.get("column_names") or ())
            for item in (
                inspect(sync_conn).get_unique_constraints("workspaces")
                + [
                    index
                    for index in inspect(sync_conn).get_indexes("workspaces")
                    if index.get("unique")
                ]
            )
        }
    )
    columns = ("node_id", "accelerator_device_id", "accelerator_slot")
    if columns not in unique_sets:
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX uq_workspaces_accelerator_slot "
                "ON workspaces "
                "(node_id, accelerator_device_id, accelerator_slot)"
            )
        )


async def _add_workspace_name_uniqueness(conn) -> None:
    """Scope workspace names to their owner.

    Names were previously unconstrained, so one user could own several
    identically named workspaces. Legacy duplicates are disambiguated with a
    numeric suffix before the constraint is created; the display name is
    updated alongside the key so the dashboard does not show two rows that
    look identical.
    """
    from app.models.workspace import normalize_workspace_name

    columns = await conn.run_sync(
        lambda sync_conn: {
            column["name"] for column in inspect(sync_conn).get_columns("workspaces")
        }
    )
    if "name_key" not in columns:
        await conn.execute(
            text(
                "ALTER TABLE workspaces ADD COLUMN name_key "
                "VARCHAR(100) NOT NULL DEFAULT ''"
            )
        )

    rows = (
        await conn.execute(
            text("SELECT id, user_id, name FROM workspaces ORDER BY created_at, id")
        )
    ).all()
    claimed: set[tuple[int, str]] = set()
    for workspace_id, user_id, name in rows:
        original = str(name or "").strip()
        display = original or f"workspace-{str(workspace_id)[:8]}"
        candidate = display
        key = normalize_workspace_name(candidate)
        suffix = 2
        while (user_id, key) in claimed:
            candidate = f"{display} ({suffix})"[:100]
            key = normalize_workspace_name(candidate)
            suffix += 1
        claimed.add((user_id, key))
        await conn.execute(
            text(
                "UPDATE workspaces SET name = :name, name_key = :name_key "
                "WHERE id = :id"
            ),
            {"name": candidate, "name_key": key[:100], "id": workspace_id},
        )

    unique_sets = await conn.run_sync(
        lambda sync_conn: {
            tuple(item.get("column_names") or ())
            for item in (
                inspect(sync_conn).get_unique_constraints("workspaces")
                + [
                    index
                    for index in inspect(sync_conn).get_indexes("workspaces")
                    if index.get("unique")
                ]
            )
        }
    )
    if ("user_id", "name_key") not in unique_sets:
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX uq_workspaces_user_name "
                "ON workspaces (user_id, name_key)"
            )
        )


async def _add_jupyter_ai_model_catalog(conn) -> None:
    """Add the centrally managed multi-model catalog to existing databases."""
    from app.jupyter_ai import default_model_catalog, parse_model_catalog

    columns = await conn.run_sync(
        lambda sync_conn: {
            column["name"]
            for column in inspect(sync_conn).get_columns("jupyter_ai_settings")
        }
    )
    if "gateway_model_discovery" not in columns:
        await conn.execute(
            text(
                "ALTER TABLE jupyter_ai_settings ADD COLUMN "
                "gateway_model_discovery BOOLEAN NOT NULL DEFAULT false"
            )
        )
    if "model_catalog_json" not in columns:
        await conn.execute(
            text(
                "ALTER TABLE jupyter_ai_settings ADD COLUMN "
                "model_catalog_json TEXT NOT NULL DEFAULT '[]'"
            )
        )
    rows = (
        await conn.execute(
            text("SELECT id, model_id, model_catalog_json FROM jupyter_ai_settings")
        )
    ).all()
    for row in rows:
        catalog = parse_model_catalog(row.model_catalog_json, row.model_id)
        if len(catalog) <= 1:
            catalog = default_model_catalog()
            if row.model_id and row.model_id not in {
                item["model_id"] for item in catalog
            }:
                catalog.insert(
                    0,
                    {
                        "model_id": row.model_id,
                        "name": row.model_id,
                        "description": "Default",
                    },
                )
        await conn.execute(
            text(
                "UPDATE jupyter_ai_settings SET model_catalog_json = :catalog "
                "WHERE id = :id"
            ),
            {"id": row.id, "catalog": json.dumps(catalog, ensure_ascii=False)},
        )


async def _add_genai_teams_and_workspace_keys(conn) -> None:
    """Add LiteLLM team settings and per-user workspace key columns."""
    wanted = {
        "genai_settings": {
            "default_team": "VARCHAR(255) NOT NULL DEFAULT ''",
            "team_priority_json": "TEXT NOT NULL DEFAULT '[]'",
        },
        "genai_accounts": {
            "personal_key_team": "VARCHAR(255) NOT NULL DEFAULT ''",
            "workspace_key_alias": "VARCHAR(255) NOT NULL DEFAULT ''",
            "workspace_key_token": "VARCHAR(255) NOT NULL DEFAULT ''",
            "workspace_key_team": "VARCHAR(255) NOT NULL DEFAULT ''",
            "encrypted_workspace_key": "TEXT NOT NULL DEFAULT ''",
        },
    }
    for table, columns in wanted.items():
        existing = await conn.run_sync(
            lambda sync_conn, table=table: {
                column["name"] for column in inspect(sync_conn).get_columns(table)
            }
        )
        for name, definition in columns.items():
            if name not in existing:
                await conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                )


async def _add_directory_manager_chain_fields(conn) -> None:
    """Add the müdürlük-from-manager-chain settings and the unit-head column."""
    wanted = {
        "directory_settings": {
            "manager_attribute": "VARCHAR(128) NOT NULL DEFAULT 'manager'",
            "title_attribute": "VARCHAR(128) NOT NULL DEFAULT 'title'",
            "unit_head_titles": "VARCHAR(512) NOT NULL DEFAULT 'MÜDÜR'",
        },
        "users": {
            "managed_unit": "VARCHAR(255) NOT NULL DEFAULT ''",
        },
    }
    for table, columns in wanted.items():
        existing = await conn.run_sync(
            lambda sync_conn, table=table: {
                column["name"] for column in inspect(sync_conn).get_columns(table)
            }
        )
        for name, definition in columns.items():
            if name not in existing:
                await conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                )


async def _add_directory_sync_fields(conn) -> None:
    """Add the Genel Müdür titles and the last bulk sync result."""
    wanted = {
        "division_head_titles": "VARCHAR(512) NOT NULL DEFAULT 'GENEL MÜDÜR'",
        "last_sync_at": "TIMESTAMP WITH TIME ZONE",
        "last_sync_summary": "TEXT NOT NULL DEFAULT ''",
    }
    existing = await conn.run_sync(
        lambda sync_conn: {
            column["name"]
            for column in inspect(sync_conn).get_columns("directory_settings")
        }
    )
    for name, definition in wanted.items():
        if name not in existing:
            await conn.execute(
                text(f"ALTER TABLE directory_settings ADD COLUMN {name} {definition}")
            )


async def _rename_migrations_table(conn) -> None:
    if await migrations_table(conn) == LEGACY_MIGRATIONS_TABLE:
        await conn.execute(
            text(f"ALTER TABLE {LEGACY_MIGRATIONS_TABLE} RENAME TO {MIGRATIONS_TABLE}")
        )


LEGACY_IMAGE_PREFIX = "localhost/devcloud-"
CURRENT_IMAGE_PREFIX = "localhost/aifactory-"
LEGACY_STORAGE_PREFIX = "/var/lib/devcloud/"
CURRENT_STORAGE_PREFIX = "/var/lib/aifactory/"


async def _rename_devcloud_values(conn) -> None:
    """Point stored image tags and workspace storage at the aifactory names.

    Workers tag their loaded images under the new name (app.worker_agent),
    and the 3.49 move renamed /var/lib/devcloud on every host. Registry
    references keep their names: those images already exist under them.
    """
    for table, column, old, new in (
        ("workspace_images", "image_ref", LEGACY_IMAGE_PREFIX, CURRENT_IMAGE_PREFIX),
        ("custom_templates", "image_tag", LEGACY_IMAGE_PREFIX, CURRENT_IMAGE_PREFIX),
        ("workspaces", "storage_path", LEGACY_STORAGE_PREFIX, CURRENT_STORAGE_PREFIX),
    ):
        await conn.execute(
            text(
                f"UPDATE {table} SET {column} = :new || SUBSTR({column}, :start) "
                f"WHERE SUBSTR({column}, 1, :length) = :old"
            ),
            {"new": new, "old": old, "start": len(old) + 1, "length": len(old)},
        )


async def _add_genai_auto_provision(conn) -> None:
    """Add the switch for LiteLLM provisioning at AD login (on by default)."""
    existing = await conn.run_sync(
        lambda sync_conn: {
            column["name"] for column in inspect(sync_conn).get_columns("genai_settings")
        }
    )
    if "auto_provision_directory_users" not in existing:
        await conn.execute(
            text(
                "ALTER TABLE genai_settings ADD COLUMN "
                "auto_provision_directory_users BOOLEAN NOT NULL DEFAULT TRUE"
            )
        )


async def _remove_datacenters(conn) -> None:
    """Drop the datacenter placement (IDM/ODM); custom tags stay."""
    await conn.execute(text("DELETE FROM placement_tags WHERE kind = 'datacenter'"))
    for table in ("nodes", "workspaces"):
        columns = await conn.run_sync(
            lambda sync_conn, table=table: {
                column["name"] for column in inspect(sync_conn).get_columns(table)
            }
        )
        if "datacenter" in columns:
            await conn.execute(text(f"ALTER TABLE {table} DROP COLUMN datacenter"))


async def _replace_tcmb_code_with_kilo_code(conn) -> None:
    """Move TCMB Code settings and workspaces to upstream Kilo Code.

    * ``tcmb_code_settings`` becomes ``kilo_code_settings`` (init_db created
      it); the TCMB Library URL is reused as the catalog URL.
    * ``vscode-tcmb`` workspaces and display overrides become ``vscode-kilo``.
    * Every vscode-tcmb and vscode-cuda image ships TCMB Code, so those image
      records are disabled and their workspaces unpinned, with a config digest
      no rendered config has. A workspace keeps its old container until the
      template's new Kilo Code image has synchronized to its worker, then its
      next start pins that image and recreates the container (project files
      are kept).
    """
    tables = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
    if "tcmb_code_settings" in tables:
        existing = await conn.execute(text("SELECT id FROM kilo_code_settings"))
        if existing.first() is None:
            await conn.execute(
                text(
                    "INSERT INTO kilo_code_settings (id, enabled, catalog_url, "
                    "encrypted_catalog_token, skills_urls_json, model_ids_json, "
                    "indexing_enabled, indexing_model, indexing_dimension, "
                    "encrypted_indexing_key, updated_at) "
                    "SELECT id, enabled, library_url, '', '[]', model_ids_json, "
                    "indexing_enabled, indexing_model, indexing_dimension, "
                    "encrypted_indexing_key, updated_at FROM tcmb_code_settings"
                )
            )
        await conn.execute(text("DROP TABLE tcmb_code_settings"))
        # The catalog URL is a root; the controller appends /mcp itself.
        for row in (
            await conn.execute(text("SELECT id, catalog_url FROM kilo_code_settings"))
        ).all():
            url = str(row[1] or "").strip().rstrip("/")
            if url.lower().endswith("/mcp"):
                url = url[: -len("/mcp")].rstrip("/")
            if url != row[1]:
                await conn.execute(
                    text("UPDATE kilo_code_settings SET catalog_url = :url WHERE id = :id"),
                    {"url": url, "id": row[0]},
                )
    await conn.execute(
        text(
            "UPDATE workspaces SET template_id = 'vscode-kilo' "
            "WHERE template_id = 'vscode-tcmb'"
        )
    )
    # No rendered config has this digest, so the next start with the new image
    # on the worker recreates the container even when Kilo Code renders none.
    await conn.execute(
        text(
            "UPDATE workspaces SET image_id = NULL, ai_config_digest = :digest "
            "WHERE template_id IN ('vscode-kilo', 'vscode-cuda')"
        ),
        {"digest": TCMB_CODE_MIGRATED_DIGEST},
    )
    await conn.execute(
        text(
            "UPDATE workspace_images SET enabled = :disabled "
            "WHERE template_id IN ('vscode-tcmb', 'vscode-cuda')"
        ),
        {"disabled": False},
    )
    renamed = await conn.execute(
        text("SELECT template_id FROM template_settings WHERE template_id = 'vscode-kilo'")
    )
    if renamed.first() is None:
        await conn.execute(
            text(
                "UPDATE template_settings SET template_id = 'vscode-kilo' "
                "WHERE template_id = 'vscode-tcmb'"
            )
        )
    else:
        await conn.execute(
            text("DELETE FROM template_settings WHERE template_id = 'vscode-tcmb'")
        )
    # Admin display overrides still naming TCMB Code would hide the new name.
    for column in ("name", "description"):
        await conn.execute(
            text(
                f"UPDATE template_settings SET {column} = '' "
                "WHERE template_id IN ('vscode-kilo', 'vscode-cuda') "
                f"AND LOWER({column}) LIKE '%tcmb code%'"
            )
        )


async def _add_jupyter_ai_cline_toggle(conn) -> None:
    """Add the Cline toggle; preserve existing Admin choices on upgrade."""
    columns = await conn.run_sync(
        lambda sync_conn: {
            column["name"]
            for column in inspect(sync_conn).get_columns("jupyter_ai_settings")
        }
    )
    if "cline_enabled" not in columns:
        await conn.execute(
            text(
                "ALTER TABLE jupyter_ai_settings ADD COLUMN "
                "cline_enabled BOOLEAN NOT NULL DEFAULT true"
            )
        )


async def _migrate_mlflow_server_settings(conn) -> bool:
    """Adopt one unambiguous legacy per-user server policy."""
    tables = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
    if not {"mlflow_settings", "mlflow_server_settings"} <= tables:
        return False
    existing = (
        await conn.execute(text("SELECT id FROM mlflow_server_settings WHERE id = 1"))
    ).scalar_one_or_none()
    if existing is not None:
        return False
    rows = (
        await conn.execute(
            text(
                "SELECT DISTINCT base_url, validate_tls, ca_cert_file, timeout_seconds "
                "FROM mlflow_settings WHERE base_url IS NOT NULL AND base_url <> ''"
            )
        )
    ).all()
    policies = {
        (
            str(row[0]).strip().rstrip("/"),
            bool(row[1]),
            str(row[2] or ""),
            int(row[3] or 10),
        )
        for row in rows
        if str(row[0]).strip()
    }
    if len(policies) != 1:
        return False
    base_url, validate_tls, ca_cert_file, timeout_seconds = policies.pop()
    any_enabled = bool(
        (
            await conn.execute(
                text("SELECT COUNT(*) FROM mlflow_settings WHERE enabled = :enabled"),
                {"enabled": True},
            )
        ).scalar_one()
    )
    await conn.execute(
        text(
            "INSERT INTO mlflow_server_settings "
            "(id, enabled, base_url, validate_tls, ca_cert_file, timeout_seconds, updated_at) "
            "VALUES (1, :enabled, :base_url, :validate_tls, :ca_cert_file, "
            ":timeout_seconds, CURRENT_TIMESTAMP)"
        ),
        {
            "enabled": any_enabled,
            "base_url": base_url,
            "validate_tls": validate_tls,
            "ca_cert_file": ca_cert_file,
            "timeout_seconds": timeout_seconds,
        },
    )
    return True


async def _add_mlflow_deployment_columns(conn) -> None:
    """Add immutable image ownership/pinning fields to existing installations."""
    workspace_columns = await conn.run_sync(
        lambda sync_conn: {
            column["name"] for column in inspect(sync_conn).get_columns("workspaces")
        }
    )
    if "image_id" not in workspace_columns:
        await conn.execute(text("ALTER TABLE workspaces ADD COLUMN image_id VARCHAR(36)"))
    image_columns = await conn.run_sync(
        lambda sync_conn: {
            column["name"] for column in inspect(sync_conn).get_columns("workspace_images")
        }
    )
    if "purpose" not in image_columns:
        await conn.execute(
            text(
                "ALTER TABLE workspace_images ADD COLUMN purpose "
                "VARCHAR(32) NOT NULL DEFAULT 'template'"
            )
        )
    if "owner_user_id" not in image_columns:
        await conn.execute(
            text("ALTER TABLE workspace_images ADD COLUMN owner_user_id INTEGER")
        )
    deployment_columns = await conn.run_sync(
        lambda sync_conn: {
            column["name"]
            for column in inspect(sync_conn).get_columns("mlflow_deployments")
        }
    )
    if "lease_owner" not in deployment_columns:
        await conn.execute(
            text(
                "ALTER TABLE mlflow_deployments ADD COLUMN lease_owner "
                "VARCHAR(128) NOT NULL DEFAULT ''"
            )
        )
    if "lease_expires_at" not in deployment_columns:
        await conn.execute(
            text(
                "ALTER TABLE mlflow_deployments ADD COLUMN lease_expires_at TIMESTAMP"
            )
        )
    if "quota_reserved" not in deployment_columns:
        await conn.execute(
            text(
                "ALTER TABLE mlflow_deployments ADD COLUMN quota_reserved "
                "BOOLEAN NOT NULL DEFAULT false"
            )
        )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_mlflow_deployments_quota_reserved "
            "ON mlflow_deployments (quota_reserved)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_mlflow_deployments_lease_expires_at "
            "ON mlflow_deployments (lease_expires_at)"
        )
    )
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_workspaces_image_id ON workspaces (image_id)")
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_workspace_images_owner_user_id "
            "ON workspace_images (owner_user_id)"
        )
    )


async def _enforce_mlflow_image_references(conn) -> None:
    """Enforce image pin integrity on upgraded SQLite and PostgreSQL schemas."""
    if conn.dialect.name == "sqlite":
        await conn.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS fk_workspaces_image_insert "
                "BEFORE INSERT ON workspaces WHEN NEW.image_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM workspace_images WHERE id = NEW.image_id) "
                "BEGIN SELECT RAISE(ABORT, 'workspace image foreign key violation'); END"
            )
        )
        await conn.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS fk_workspaces_image_update "
                "BEFORE UPDATE OF image_id ON workspaces WHEN NEW.image_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM workspace_images WHERE id = NEW.image_id) "
                "BEGIN SELECT RAISE(ABORT, 'workspace image foreign key violation'); END"
            )
        )
        await conn.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS fk_workspace_images_delete_restrict "
                "BEFORE DELETE ON workspace_images WHEN EXISTS "
                "(SELECT 1 FROM workspaces WHERE image_id = OLD.id) "
                "BEGIN SELECT RAISE(ABORT, 'workspace image is pinned'); END"
            )
        )
        await conn.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS fk_workspace_images_owner_insert "
                "BEFORE INSERT ON workspace_images WHEN NEW.owner_user_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.owner_user_id) "
                "BEGIN SELECT RAISE(ABORT, 'workspace image owner foreign key violation'); END"
            )
        )
        await conn.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS fk_workspace_images_owner_update "
                "BEFORE UPDATE OF owner_user_id ON workspace_images "
                "WHEN NEW.owner_user_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.owner_user_id) "
                "BEGIN SELECT RAISE(ABORT, 'workspace image owner foreign key violation'); END"
            )
        )
        return
    if conn.dialect.name == "postgresql":
        foreign_keys = await conn.run_sync(
            lambda sync_conn: {
                tuple(item.get("constrained_columns") or ())
                for item in inspect(sync_conn).get_foreign_keys("workspaces")
            }
        )
        if ("image_id",) not in foreign_keys:
            await conn.execute(
                text(
                    "ALTER TABLE workspaces ADD CONSTRAINT fk_workspaces_image_id "
                    "FOREIGN KEY (image_id) REFERENCES workspace_images(id) ON DELETE RESTRICT"
                )
            )
        image_foreign_keys = await conn.run_sync(
            lambda sync_conn: {
                tuple(item.get("constrained_columns") or ())
                for item in inspect(sync_conn).get_foreign_keys("workspace_images")
            }
        )
        if ("owner_user_id",) not in image_foreign_keys:
            await conn.execute(
                text(
                    "ALTER TABLE workspace_images ADD CONSTRAINT "
                    "fk_workspace_images_owner_user_id FOREIGN KEY (owner_user_id) "
                    "REFERENCES users(id) ON DELETE CASCADE"
                )
            )


async def _add_image_release_bamboo_columns(conn) -> None:
    """Track the Bamboo retag plan queued after each published release."""
    wanted = {
        "bamboo_status": "VARCHAR(16) NOT NULL DEFAULT ''",
        "bamboo_result_key": "VARCHAR(128) NOT NULL DEFAULT ''",
        "bamboo_result_url": "VARCHAR(512) NOT NULL DEFAULT ''",
        "bamboo_error": "TEXT",
        "bamboo_attempted_at": "TIMESTAMP WITH TIME ZONE",
    }
    existing = await conn.run_sync(
        lambda sync_conn: {
            column["name"]
            for column in inspect(sync_conn).get_columns("mlflow_image_releases")
        }
    )
    for name, definition in wanted.items():
        if name not in existing:
            await conn.execute(
                text(f"ALTER TABLE mlflow_image_releases ADD COLUMN {name} {definition}")
            )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_mlflow_image_releases_bamboo_status "
            "ON mlflow_image_releases (bamboo_status)"
        )
    )


async def _add_jupyter_ai_ca_bundle(conn) -> None:
    """Store the gateway CA bundle that workers install in workspaces."""
    columns = await conn.run_sync(
        lambda sync_conn: {
            column["name"]
            for column in inspect(sync_conn).get_columns("jupyter_ai_settings")
        }
    )
    if "ca_bundle_pem" not in columns:
        await conn.execute(
            text(
                "ALTER TABLE jupyter_ai_settings "
                "ADD COLUMN ca_bundle_pem TEXT NOT NULL DEFAULT ''"
            )
        )


async def upgrade() -> None:
    # The legacy initializer remains the compatibility migration for all
    # pre-versioned installations.
    await init_db()
    async with engine.begin() as conn:
        await _rename_migrations_table(conn)
        applied = await _applied_versions(conn)
        if 1 not in applied:
            await _record_version(conn, 1, "version existing schema")
        if 2 not in applied:
            # init_db adds all telemetry/inventory columns idempotently.
            await _record_version(conn, 2, "worker telemetry and inventory")
        if 3 not in applied:
            await _worker_only_constraints(conn)
            await _record_version(conn, 3, "worker-only workspace placement")
        if 4 not in applied:
            # init_db creates the portable workspace_images table.
            await _record_version(conn, 4, "controller-managed workspace images")
        if 5 not in applied:
            # init_db creates the portable worker_bootstrap_tickets table.
            await _record_version(conn, 5, "single-use worker bootstrap tickets")
        if 6 not in applied:
            await _make_mlflow_settings_per_user(conn)
            await _record_version(conn, 6, "per-user MLflow settings")
        if 7 not in applied:
            await _add_directory_profile_fields(conn)
            await _record_version(conn, 7, "LDAP profile organization fields")
        if 8 not in applied:
            await _add_gpu_allocation_constraints(conn)
            await _record_version(conn, 8, "GPU workspace slot allocations")
        if 9 not in applied:
            # init_db creates the portable singleton settings table.
            await _record_version(conn, 9, "central Jupyter AI gateway settings")
        if 10 not in applied:
            await _add_jupyter_ai_model_catalog(conn)
            await _record_version(conn, 10, "Jupyter AI multi-model catalog")
        if 11 not in applied:
            # init_db creates the portable per-flavor availability table.
            await _record_version(conn, 11, "admin-managed flavor availability")
        if 12 not in applied:
            await _sync_mlflow_settings_id_sequence(conn)
            await _record_version(conn, 12, "create MLflow settings ID sequence")
        if 13 not in applied:
            await _expand_flavor_settings(conn)
            await _record_version(conn, 13, "custom and editable flavor profiles")
        if 14 not in applied:
            await _add_jupyter_ai_cline_toggle(conn)
            await _record_version(conn, 14, "admin-managed Cline availability")
        if 15 not in applied:
            # init_db creates the portable singleton settings table.
            await _migrate_mlflow_server_settings(conn)
            await _record_version(conn, 15, "admin-managed MLflow server")
        if 16 not in applied:
            # init_db creates the deployment/build/event tables portably.
            await _add_mlflow_deployment_columns(conn)
            await _record_version(conn, 16, "MLflow model deployments and pinned images")
        if 17 not in applied:
            await _add_mlflow_deployment_columns(conn)
            await _record_version(conn, 17, "durable MLflow deployment leases")
        if 18 not in applied:
            await _add_mlflow_deployment_columns(conn)
            await _record_version(conn, 18, "MLflow deployment quota reservations")
        if 19 not in applied:
            await _enforce_mlflow_image_references(conn)
            await _record_version(conn, 19, "enforce MLflow image pin references")
        if 20 not in applied:
            # init_db creates the portable encrypted registry singleton table.
            await _record_version(conn, 20, "admin-managed model container registry")
        if 21 not in applied:
            await _add_directory_hierarchy_fields(conn)
            await _record_version(conn, 21, "LDAP organization hierarchy")
        if 22 not in applied:
            # init_db creates the portable onboarding settings and progress tables.
            await _record_version(conn, 22, "versioned onboarding tour")
        if 23 not in applied:
            # init_db adds the optional worker IPv4 route fallback column.
            await _record_version(conn, 23, "worker FQDN route fallback")
        if 24 not in applied:
            await _add_workspace_name_uniqueness(conn)
            await _record_version(conn, 24, "per-owner workspace names")
        if 25 not in applied:
            # init_db creates the portable user_group_quotas table.
            await _add_team_quota_groups(conn)
            await _record_version(conn, 25, "team quota groups and user overrides")
        if 26 not in applied:
            # init_db creates the portable genai_settings and genai_accounts tables.
            await _record_version(conn, 26, "GenAI LiteLLM self-service keys")
        if 27 not in applied:
            await _add_genai_teams_and_workspace_keys(conn)
            await _record_version(conn, 27, "GenAI teams and per-user workspace keys")
        if 28 not in applied:
            await _add_directory_manager_chain_fields(conn)
            await _record_version(conn, 28, "müdürlük hierarchy from the AD manager chain")
        if 29 not in applied:
            # init_db creates the portable directory_teams table.
            await _add_directory_sync_fields(conn)
            await _record_version(conn, 29, "bulk AD sync and directory teams")
        if 30 not in applied:
            # init_db created the (since dropped) tcmb_code_settings table and the
            # workspaces.ai_config_digest column.
            await _record_version(conn, 30, "TCMB Code settings and workspace AI config digest")
        if 31 not in applied:
            # init_db creates placement_tags and the node/workspace tags_json
            # columns. The IDM/ODM datacenter tags it seeded were removed in 37.
            await _record_version(conn, 31, "datacenter and placement tags")
        if 32 not in applied:
            # init_db creates the portable deploy_registry_settings, k8s_projects,
            # k8s_applications, mlflow_image_releases and event tables.
            await _record_version(conn, 32, "Kubernetes image releases to the deploy registry")
        if 33 not in applied:
            # init_db creates the portable kilo_code_settings table.
            await _replace_tcmb_code_with_kilo_code(conn)
            await _record_version(conn, 33, "Kilo Code replaces TCMB Code")
        if 34 not in applied:
            # init_db creates the portable bamboo_settings table.
            await _add_image_release_bamboo_columns(conn)
            await _record_version(conn, 34, "Bamboo retag trigger after image releases")
        if 35 not in applied:
            await _add_jupyter_ai_ca_bundle(conn)
            await _record_version(conn, 35, "Workspace AI gateway CA bundle")
        if 36 not in applied:
            # init_db creates the portable skills table.
            await _record_version(conn, 36, "Admin skill library for Kilo Code and Jupyter AI")
        if 37 not in applied:
            await _remove_datacenters(conn)
            await _record_version(conn, 37, "remove datacenter (IDM/ODM) placement")
        if 38 not in applied:
            await _add_genai_auto_provision(conn)
            await _record_version(conn, 38, "GenAI auto-provisioning at AD login")
        if 39 not in applied:
            await _rename_devcloud_values(conn)
            await _record_version(conn, 39, "aifactory image tags and storage paths")
        if 40 not in applied:
            # init_db creates the portable logging_settings table.
            await _record_version(conn, 40, "log retention and forwarding settings")


async def current_version() -> int:
    async with engine.begin() as conn:
        table = await migrations_table(conn)
        if table is None:
            return 0
        value = (
            await conn.execute(text(f"SELECT MAX(version) FROM {table}"))
        ).scalar_one_or_none()
        return int(value or 0)


async def require_current() -> None:
    version = await current_version()
    if version != CURRENT_SCHEMA_VERSION:
        raise MigrationError(
            f"Database schema is version {version}; expected "
            f"{CURRENT_SCHEMA_VERSION}. Run 'python -m app.migrations upgrade'."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.migrations")
    parser.add_argument("command", choices=["upgrade", "current", "check"])
    args = parser.parse_args(argv)
    if args.command == "upgrade":
        asyncio.run(upgrade())
        return 0
    version = asyncio.run(current_version())
    if args.command == "current":
        print(version)
        return 0
    if version != CURRENT_SCHEMA_VERSION:
        print(f"schema version {version}; expected {CURRENT_SCHEMA_VERSION}")
        return 1
    print(f"schema version {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
