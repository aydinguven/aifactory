import asyncio
import json
import os
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.database import (
    ensure_download_settings_columns,
    ensure_user_quota_columns,
    ensure_workspace_columns,
)
from app.migrations import (
    _add_jupyter_ai_cline_toggle,
    _add_directory_profile_fields,
    _add_jupyter_ai_model_catalog,
    _add_workspace_name_uniqueness,
    _make_mlflow_settings_per_user,
    _migrate_mlflow_server_settings,
    _sync_mlflow_settings_id_sequence,
)


@pytest.mark.asyncio
async def test_unambiguous_legacy_mlflow_url_becomes_admin_managed(tmp_path):
    database_path = (tmp_path / "managed-mlflow.db").as_posix()
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE mlflow_settings ("
                    "id INTEGER PRIMARY KEY, enabled BOOLEAN NOT NULL, "
                    "base_url VARCHAR(1024) NOT NULL, validate_tls BOOLEAN NOT NULL, "
                    "ca_cert_file VARCHAR(512) NOT NULL, timeout_seconds INTEGER NOT NULL)"
                )
            )
            await conn.execute(
                text(
                    "CREATE TABLE mlflow_server_settings ("
                    "id INTEGER PRIMARY KEY, enabled BOOLEAN NOT NULL, "
                    "base_url VARCHAR(1024) NOT NULL, validate_tls BOOLEAN NOT NULL, "
                    "ca_cert_file VARCHAR(512) NOT NULL, timeout_seconds INTEGER NOT NULL, "
                    "updated_at DATETIME NOT NULL)"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO mlflow_settings VALUES "
                    "(1, 1, 'https://mlflow.internal/', 1, '', 15), "
                    "(2, 1, 'https://mlflow.internal', 1, '', 15)"
                )
            )
            assert await _migrate_mlflow_server_settings(conn) is True
            row = (
                await conn.execute(
                    text(
                        "SELECT enabled, base_url, validate_tls, timeout_seconds "
                        "FROM mlflow_server_settings WHERE id = 1"
                    )
                )
            ).one()
    finally:
        await engine.dispose()
    assert row == (1, "https://mlflow.internal", 1, 15)


@pytest.mark.asyncio
async def test_conflicting_legacy_mlflow_urls_require_admin_choice(tmp_path):
    database_path = (tmp_path / "conflicting-mlflow.db").as_posix()
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE mlflow_settings ("
                    "id INTEGER PRIMARY KEY, enabled BOOLEAN NOT NULL, "
                    "base_url VARCHAR(1024) NOT NULL, validate_tls BOOLEAN NOT NULL, "
                    "ca_cert_file VARCHAR(512) NOT NULL, timeout_seconds INTEGER NOT NULL)"
                )
            )
            await conn.execute(
                text(
                    "CREATE TABLE mlflow_server_settings ("
                    "id INTEGER PRIMARY KEY, enabled BOOLEAN NOT NULL, "
                    "base_url VARCHAR(1024) NOT NULL, validate_tls BOOLEAN NOT NULL, "
                    "ca_cert_file VARCHAR(512) NOT NULL, timeout_seconds INTEGER NOT NULL, "
                    "updated_at DATETIME NOT NULL)"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO mlflow_settings VALUES "
                    "(1, 1, 'https://one.internal', 1, '', 10), "
                    "(2, 1, 'https://two.internal', 1, '', 10)"
                )
            )
            assert await _migrate_mlflow_server_settings(conn) is False
            count = (
                await conn.execute(text("SELECT COUNT(*) FROM mlflow_server_settings"))
            ).scalar_one()
    finally:
        await engine.dispose()
    assert count == 0


@pytest.mark.asyncio
async def test_existing_users_table_receives_quota_columns(tmp_path):
    database_path = (tmp_path / "legacy.db").as_posix()
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE users ("
                    "id INTEGER PRIMARY KEY, "
                    "username VARCHAR(64) NOT NULL"
                    ")"
                )
            )
            await conn.execute(
                text("INSERT INTO users (id, username) VALUES (1, 'legacy')")
            )

        async def run_migration():
            async with engine.begin() as migration_conn:
                await ensure_user_quota_columns(migration_conn)

        await asyncio.gather(run_migration(), run_migration())

        async with engine.connect() as conn:
            columns = await conn.run_sync(
                lambda sync_conn: {
                    column["name"]
                    for column in inspect(sync_conn).get_columns("users")
                }
            )
            row = (
                await conn.execute(
                    text(
                        "SELECT cpu_quota, memory_mb_quota, disk_mb_quota "
                        "FROM users WHERE id = 1"
                    )
                )
            ).one()
    finally:
        await engine.dispose()

    assert {"cpu_quota", "memory_mb_quota", "disk_mb_quota"} <= columns
    assert row == (
        settings.DEFAULT_USER_CPU_QUOTA,
        settings.DEFAULT_USER_MEMORY_MB_QUOTA,
        settings.DEFAULT_USER_DISK_MB_QUOTA,
    )


@pytest.mark.asyncio
async def test_existing_workspaces_table_receives_node_id_column(tmp_path):
    database_path = (tmp_path / "legacy-workspaces.db").as_posix()
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE workspaces (id VARCHAR(36) PRIMARY KEY)"))
            await ensure_workspace_columns(conn)
            columns = await conn.run_sync(
                lambda sync_conn: {column["name"] for column in inspect(sync_conn).get_columns("workspaces")}
            )
    finally:
        await engine.dispose()
    assert "node_id" in columns


@pytest.mark.asyncio
async def test_existing_download_settings_table_receives_https_columns(tmp_path):
    database_path = (tmp_path / "legacy-download-settings.db").as_posix()
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE download_settings ("
                    "id INTEGER PRIMARY KEY, "
                    "public_base_url VARCHAR(1024) NOT NULL"
                    ")"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO download_settings (id, public_base_url) "
                    "VALUES (1, 'http://10.253.6.189')"
                )
            )
            await ensure_download_settings_columns(conn)
            columns = await conn.run_sync(
                lambda sync_conn: {
                    column["name"]
                    for column in inspect(sync_conn).get_columns("download_settings")
                }
            )
            row = (
                await conn.execute(
                    text(
                        "SELECT https_enabled, https_hostname, "
                        "http_fallback_enabled FROM download_settings WHERE id = 1"
                    )
                )
            ).one()
    finally:
        await engine.dispose()

    assert {
        "https_enabled",
        "https_hostname",
        "http_fallback_enabled",
        "certificate_subject",
        "certificate_not_after",
        "certificate_sha256",
    } <= columns
    assert row == (0, settings.HTTPS_DEFAULT_HOSTNAME, 1)


@pytest.mark.asyncio
async def test_legacy_mlflow_settings_receive_user_ownership(tmp_path):
    database_path = (tmp_path / "legacy-mlflow.db").as_posix()
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE mlflow_settings ("
                    "id INTEGER PRIMARY KEY, "
                    "encrypted_secret TEXT NOT NULL)"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO mlflow_settings (id, encrypted_secret) "
                    "VALUES (1, 'legacy-encrypted-value')"
                )
            )
            await _make_mlflow_settings_per_user(conn)
            await _make_mlflow_settings_per_user(conn)
            columns = await conn.run_sync(
                lambda sync_conn: {
                    column["name"]
                    for column in inspect(sync_conn).get_columns(
                        "mlflow_settings"
                    )
                }
            )
            unique_sets = await conn.run_sync(
                lambda sync_conn: {
                    tuple(index.get("column_names") or ())
                    for index in inspect(sync_conn).get_indexes(
                        "mlflow_settings"
                    )
                    if index.get("unique")
                }
            )
            legacy_user_id = (
                await conn.execute(
                    text("SELECT user_id FROM mlflow_settings WHERE id = 1")
                )
            ).scalar_one()
    finally:
        await engine.dispose()

    assert "user_id" in columns
    assert ("user_id",) in unique_sets
    assert legacy_user_id is None


@pytest.mark.asyncio
async def test_postgresql_mlflow_sequence_repair_uses_expected_statements():
    class FakeConnection:
        dialect = SimpleNamespace(name="postgresql")

        def __init__(self):
            self.statements: list[str] = []

        async def execute(self, statement):
            self.statements.append(str(statement))

    conn = FakeConnection()
    await _sync_mlflow_settings_id_sequence(conn)

    assert len(conn.statements) == 4
    assert conn.statements[0] == (
        "CREATE SEQUENCE IF NOT EXISTS mlflow_settings_id_seq"
    )
    assert "OWNED BY mlflow_settings.id" in conn.statements[1]
    assert "SET DEFAULT nextval('mlflow_settings_id_seq')" in conn.statements[2]
    assert "COALESCE(MAX(id), 1)" in conn.statements[3]
    assert "MAX(id) IS NOT NULL" in conn.statements[3]


@pytest.mark.asyncio
async def test_legacy_postgresql_mlflow_table_gets_working_id_sequence():
    database_url = os.environ.get("TEST_POSTGRESQL_URL")
    if not database_url:
        pytest.skip("TEST_POSTGRESQL_URL is not configured")

    schema = "test_mlflow_sequence_migration"
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
            await conn.execute(text(f"CREATE SCHEMA {schema}"))
            await conn.execute(text(f"SET LOCAL search_path TO {schema}"))
            await conn.execute(
                text(
                    "CREATE TABLE mlflow_settings ("
                    "id INTEGER NOT NULL PRIMARY KEY, "
                    "encrypted_secret TEXT NOT NULL)"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO mlflow_settings (id, encrypted_secret) "
                    "VALUES (1, 'legacy-encrypted-value')"
                )
            )

            await _sync_mlflow_settings_id_sequence(conn)

            inserted_id = (
                await conn.execute(
                    text(
                        "INSERT INTO mlflow_settings (encrypted_secret) "
                        "VALUES ('new-encrypted-value') RETURNING id"
                    )
                )
            ).scalar_one()
            sequence_name = (
                await conn.execute(
                    text(
                        "SELECT pg_get_serial_sequence("
                        "'mlflow_settings', 'id')"
                    )
                )
            ).scalar_one()
        assert inserted_id == 2
        assert sequence_name.endswith(".mlflow_settings_id_seq")
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await engine.dispose()


@pytest.mark.asyncio
async def test_sqlite_mlflow_sequence_repair_is_a_noop():
    class FakeConnection:
        dialect = SimpleNamespace(name="sqlite")

        async def execute(self, statement):
            raise AssertionError("SQLite must not run PostgreSQL sequence SQL")

    await _sync_mlflow_settings_id_sequence(FakeConnection())


@pytest.mark.asyncio
async def test_legacy_database_receives_directory_profile_fields(tmp_path):
    database_path = (tmp_path / "legacy-directory-profile.db").as_posix()
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
            await conn.execute(
                text("CREATE TABLE directory_settings (id INTEGER PRIMARY KEY)")
            )
            await _add_directory_profile_fields(conn)
            await _add_directory_profile_fields(conn)
            user_columns = await conn.run_sync(
                lambda sync_conn: {
                    column["name"]
                    for column in inspect(sync_conn).get_columns("users")
                }
            )
            directory_columns = await conn.run_sync(
                lambda sync_conn: {
                    column["name"]
                    for column in inspect(sync_conn).get_columns("directory_settings")
                }
            )
    finally:
        await engine.dispose()
    assert {"team", "directorate"} <= user_columns
    assert {"team_attribute", "directorate_attribute"} <= directory_columns


@pytest.mark.asyncio
async def test_legacy_jupyter_ai_settings_receive_model_catalog(tmp_path):
    database_path = (tmp_path / "legacy-jupyter-ai.db").as_posix()
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE jupyter_ai_settings ("
                    "id INTEGER PRIMARY KEY, "
                    "enabled BOOLEAN NOT NULL, "
                    "gateway_url VARCHAR(512) NOT NULL, "
                    "model_id VARCHAR(255) NOT NULL, "
                    "encrypted_shared_token TEXT NOT NULL, "
                    "updated_at DATETIME NOT NULL"
                    ")"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO jupyter_ai_settings "
                    "(id, enabled, gateway_url, model_id, "
                    "encrypted_shared_token, updated_at) VALUES "
                    "(1, true, 'https://gateway.internal', "
                    "'private-default', 'encrypted', CURRENT_TIMESTAMP)"
                )
            )
            await _add_jupyter_ai_model_catalog(conn)
            await _add_jupyter_ai_model_catalog(conn)
            await _add_jupyter_ai_cline_toggle(conn)
            await _add_jupyter_ai_cline_toggle(conn)
            columns = await conn.run_sync(
                lambda sync_conn: {
                    column["name"]
                    for column in inspect(sync_conn).get_columns(
                        "jupyter_ai_settings"
                    )
                }
            )
            row = (
                await conn.execute(
                    text(
                        "SELECT gateway_model_discovery, model_catalog_json, "
                        "cline_enabled "
                        "FROM jupyter_ai_settings WHERE id = 1"
                    )
                )
            ).one()
    finally:
        await engine.dispose()

    catalog = json.loads(row.model_catalog_json)
    assert {
        "gateway_model_discovery",
        "model_catalog_json",
        "cline_enabled",
    } <= columns
    assert row.gateway_model_discovery == 0
    assert row.cline_enabled == 1
    assert catalog[0]["model_id"] == "private-default"
    assert "qwen3.6-35b" in {item["model_id"] for item in catalog}



@pytest.mark.asyncio
async def test_legacy_duplicate_workspace_names_are_disambiguated(tmp_path):
    """Migration 24 must survive databases that already hold duplicate names."""
    database_path = (tmp_path / "duplicate-names.db").as_posix()
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE workspaces ("
                    "id VARCHAR(36) PRIMARY KEY, "
                    "name VARCHAR(100) NOT NULL, "
                    "user_id INTEGER NOT NULL, "
                    "created_at VARCHAR(64) NOT NULL)"
                )
            )
            rows = (
                ("a1", "MyWorkspace", 1, "2026-01-01T00:00:00"),
                ("a2", "myworkspace", 1, "2026-01-02T00:00:00"),
                ("a3", "  MyWorkspace  ", 1, "2026-01-03T00:00:00"),
                ("b1", "MyWorkspace", 2, "2026-01-04T00:00:00"),
                ("b2", "", 2, "2026-01-05T00:00:00"),
            )
            for row in rows:
                await conn.execute(
                    text(
                        "INSERT INTO workspaces (id, name, user_id, created_at) "
                        "VALUES (:id, :name, :user_id, :created_at)"
                    ),
                    dict(zip(("id", "name", "user_id", "created_at"), row)),
                )

            await _add_workspace_name_uniqueness(conn)

            stored = (
                await conn.execute(
                    text("SELECT id, name, name_key, user_id FROM workspaces ORDER BY id")
                )
            ).all()
            unique_sets = await conn.run_sync(
                lambda sync_conn: {
                    tuple(index.get("column_names") or ())
                    for index in inspect(sync_conn).get_indexes("workspaces")
                    if index.get("unique")
                }
            )
    finally:
        await engine.dispose()

    by_id = {row[0]: row for row in stored}
    # Each owner keeps one unsuffixed name; the collisions are numbered.
    assert by_id["a1"][2] == "myworkspace"
    assert by_id["a2"][2] == "myworkspace (2)"
    assert by_id["a3"][2] == "myworkspace (3)"
    # Display names track the key so the dashboard shows the distinction.
    assert by_id["a2"][1] == "myworkspace (2)"
    assert by_id["a3"][1] == "MyWorkspace (3)"
    # A different owner is untouched by the first owner's collisions.
    assert by_id["b1"][2] == "myworkspace"
    # An empty legacy name is replaced with a deterministic placeholder.
    assert by_id["b2"][2] == "workspace-b2"

    keys = [(row[3], row[2]) for row in stored]
    assert len(keys) == len(set(keys))
    assert ("user_id", "name_key") in unique_sets


@pytest.mark.asyncio
async def test_workspace_name_uniqueness_migration_is_idempotent(tmp_path):
    database_path = (tmp_path / "idempotent-names.db").as_posix()
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE workspaces ("
                    "id VARCHAR(36) PRIMARY KEY, "
                    "name VARCHAR(100) NOT NULL, "
                    "user_id INTEGER NOT NULL, "
                    "created_at VARCHAR(64) NOT NULL)"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO workspaces (id, name, user_id, created_at) "
                    "VALUES ('c1', 'Solo', 1, '2026-01-01T00:00:00')"
                )
            )
            await _add_workspace_name_uniqueness(conn)
            await _add_workspace_name_uniqueness(conn)

            name_key = (
                await conn.execute(text("SELECT name_key FROM workspaces"))
            ).scalar_one()
    finally:
        await engine.dispose()

    assert name_key == "solo"


@pytest.mark.asyncio
async def test_3_50_migration_renames_devcloud_table_image_tags_and_paths(tmp_path):
    from app.migrations import (
        _applied_versions,
        _rename_devcloud_values,
        _rename_migrations_table,
    )

    database_path = (tmp_path / "rename.db").as_posix()
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    try:
        async with engine.begin() as conn:
            for statement in (
                "CREATE TABLE devcloud_schema_migrations (version INTEGER PRIMARY KEY, "
                "name VARCHAR(255) NOT NULL, applied_at VARCHAR(64) NOT NULL)",
                "INSERT INTO devcloud_schema_migrations VALUES (38, 'x', 'now')",
                "CREATE TABLE workspace_images (id VARCHAR(36), image_ref VARCHAR(512))",
                "INSERT INTO workspace_images VALUES ('a', 'localhost/devcloud-vscode-kilo:latest'),"
                " ('b', 'registry.example/devcloud-mlflow-x:1')",
                "CREATE TABLE custom_templates (id VARCHAR(36), image_tag VARCHAR(255))",
                "INSERT INTO custom_templates VALUES ('c', 'localhost/devcloud-custom-rust:latest')",
                "CREATE TABLE workspaces (id VARCHAR(36), storage_path VARCHAR(512))",
                "INSERT INTO workspaces VALUES ('w', '/var/lib/devcloud/workspaces/3/w'),"
                " ('m', '/mnt/devcloud-extra/3/m')",
            ):
                await conn.execute(text(statement))
            await _rename_migrations_table(conn)
            await _rename_migrations_table(conn)
            applied = await _applied_versions(conn)
            await _rename_devcloud_values(conn)
            await _rename_devcloud_values(conn)
            tables = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            images = dict((await conn.execute(text("SELECT id, image_ref FROM workspace_images"))).all())
            tag = (await conn.execute(text("SELECT image_tag FROM custom_templates"))).scalar_one()
            paths = dict((await conn.execute(text("SELECT id, storage_path FROM workspaces"))).all())
    finally:
        await engine.dispose()

    assert applied == {38}
    assert "aifactory_schema_migrations" in tables
    assert "devcloud_schema_migrations" not in tables
    assert images == {
        "a": "localhost/aifactory-vscode-kilo:latest",
        "b": "registry.example/devcloud-mlflow-x:1",
    }
    assert tag == "localhost/aifactory-custom-rust:latest"
    assert paths == {"w": "/var/lib/aifactory/workspaces/3/w", "m": "/mnt/devcloud-extra/3/m"}
