import hashlib
import logging
import os
import time

import pytest
from sqlalchemy import update

from app.log_forwarding import ForwardingConfig, SyslogFormatter
from app.logging_setup import LoggingConfig, RetentionPolicy, apply_config, prune
from app.models.node import Node
from app.models.user import User, UserRole
from tests.conftest import TEST_WORKER_ID


@pytest.fixture
def restore_logging():
    yield
    apply_config(LoggingConfig())


async def _admin(client, db_session, username="log-admin"):
    registration = await client.post(
        "/api/auth/register",
        json={"username": username, "email": f"{username}@example.com", "password": "AdminPassword123!"},
    )
    await db_session.execute(
        update(User).where(User.id == registration.json()["user"]["id"]).values(role=UserRole.ADMIN)
    )
    await db_session.commit()
    return {"Authorization": f"Bearer {registration.json()['access_token']}"}


def _audit_events(caplog):
    return [record.event for record in caplog.records if record.name == "aifactory.audit"]


@pytest.mark.asyncio
async def test_admin_policy_reaches_enrolled_workers_only(client, db_session, restore_logging):
    headers = await _admin(client, db_session)
    body = {
        "retention_days": 7,
        "max_total_mb": 200,
        "log_level": "INFO",
        "forward_enabled": False,
        "forward_host": "qradar.example.internal",
        "forward_port": 6514,
        "forward_protocol": "tls",
        "forward_format": "leef",
    }
    assert (await client.put("/api/admin/logging-settings", json={**body, "forward_host": "https://q:514"}, headers=headers)).status_code == 422
    assert (await client.put("/api/admin/logging-settings", json={**body, "forward_enabled": True, "forward_host": ""}, headers=headers)).status_code == 422
    first = await client.put("/api/admin/logging-settings", json=body, headers=headers)
    second = await client.put("/api/admin/logging-settings", json={**body, "retention_days": 14}, headers=headers)
    assert first.status_code == second.status_code == 200
    assert second.json()["revision"] == first.json()["revision"] + 1

    endpoint = "/api/agent/logging-settings"
    assert (await client.get(endpoint, params={"node_id": TEST_WORKER_ID}, headers={"Authorization": "Bearer wrong"})).status_code == 403
    token = "worker-logging-token"
    await db_session.execute(
        update(Node).where(Node.id == TEST_WORKER_ID).values(agent_token_hash=hashlib.sha256(token.encode()).hexdigest())
    )
    await db_session.commit()
    worker = await client.get(endpoint, params={"node_id": TEST_WORKER_ID}, headers={"Authorization": f"Bearer {token}"})
    assert worker.status_code == 200
    assert worker.json()["managed"] is True
    assert worker.json()["revision"] == second.json()["revision"]
    assert worker.json()["retention_days"] == 14
    assert worker.json()["forward_host"] == "qradar.example.internal"


@pytest.mark.asyncio
async def test_api_changes_are_audited_with_field_names_but_no_values(client, db_session, caplog):
    headers = await _admin(client, db_session, "audit-admin")
    caplog.set_level(logging.INFO, logger="aifactory.audit")
    caplog.clear()

    await client.post("/api/auth/login", json={"username": "audit-admin", "password": "wrong-Password1"})
    await client.put("/api/admin/session-settings", json={"timeout_minutes": 30}, headers=headers)
    saved = await client.put(
        "/api/admin/deploy-registry-settings",
        json={"enabled": False, "registry_url": "registry.example:5000", "username": "svc", "password": "super-secret-value"},
        headers=headers,
    )
    assert saved.status_code == 200

    events = _audit_events(caplog)
    login = next(event for event in events if event["action"] == "auth.login")
    assert login["outcome"] == "failure"
    assert login["actor"] == "audit-admin"
    assert login["details"]["reason"] == "invalid_credentials"
    session = next(event for event in events if event["action"] == "PUT /api/admin/session-settings")
    assert session["actor"] == "audit-admin"
    assert session["outcome"] == "success"
    assert session["details"]["fields"] == ["timeout_minutes"]
    registry = next(event for event in events if event["action"] == "PUT /api/admin/deploy-registry-settings")
    assert "password" in registry["details"]["fields"]
    assert "super-secret-value" not in " ".join(record.getMessage() + str(record.__dict__) for record in caplog.records)
    # Login is recorded once, by its route.
    assert not any(event["action"].startswith("POST /api/auth/login") for event in events)


def test_leef_values_cannot_inject_attributes():
    formatter = SyslogFormatter(ForwardingConfig(), component="controller", hostname="host 1", version="1.0")
    record = logging.LogRecord("aifactory.audit", logging.WARNING, __file__, 0, "line1\nline2", None, None)
    record.event = {"action": "auth.login", "actor": "eve^sev=1\tcat=x|", "outcome": "failure"}
    line = formatter.format(record).decode()

    assert "\n" not in line and "\t" not in line
    assert line.startswith("<132>")
    header, _, attributes = line.partition("|^|")
    assert header.endswith("|AIFactory|AI Factory|1.0|auth.login")
    assert " host1 LEEF:2.0|" in header
    pairs = dict(item.split("=", 1) for item in attributes.split("^"))
    assert pairs["usrName"] == "eve sev=1 cat=x|"
    assert pairs["sev"] == "5"
    assert pairs["cat"] == "audit"


def test_prune_applies_retention_then_size_cap_keeping_audit_last(tmp_path):
    def make(name, megabytes, age_hours):
        path = tmp_path / name
        path.write_bytes(b"x" * int(megabytes * 1024 * 1024))
        stamp = time.time() - age_hours * 3600
        os.utime(path, (stamp, stamp))

    make("app.log", 1, 0)
    make("app.log.20260101-000000.gz", 1, 24 * 20)
    make("audit.log.20261101-000000.gz", 2, 200)
    make("app.log.20261105-000000.gz", 3, 100)
    make("access.log.20261106-000000.gz", 3, 90)
    make("app.log.20261110-000000.gz", 3, 10)
    make("unrelated.txt", 50, 24 * 30)

    removed = prune(tmp_path, RetentionPolicy(retention_days=14, max_total_bytes=8 * 1024 * 1024))

    assert removed == [
        "app.log.20260101-000000.gz",
        "app.log.20261105-000000.gz",
        "access.log.20261106-000000.gz",
    ]
    assert (tmp_path / "audit.log.20261101-000000.gz").exists()
    assert (tmp_path / "unrelated.txt").exists()
