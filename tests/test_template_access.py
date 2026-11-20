"""Workspace template access rules (app/template_access.py)."""

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update

from app.auth import ldap as ldap_module
from app.models.user import User, UserRole
from app.models.workspace import Workspace
from app.template_access import (
    AccessRule,
    AccessRuleError,
    format_entries,
    group_name,
    invalidate_cache,
    parse_entries,
    user_allowed,
)


@pytest.fixture(autouse=True)
def fresh_rule_cache():
    # Rules are cached per process; every test starts with an empty database.
    invalidate_cache()
    yield
    invalidate_cache()


def person(team="", unit="", groups=(), role=UserRole.USER):
    return SimpleNamespace(
        team=team,
        organization_unit=unit,
        directory_groups=json.dumps(list(groups)),
        role=role,
    )


def rule(allow="", deny=""):
    return AccessRule(allow=parse_entries(allow), deny=parse_entries(deny))


# ---- parsing and matching ---------------------------------------------------

def test_entries_parse_prefixes_dns_and_duplicates():
    entries = parse_entries(
        "t2-arge,  grup:T2-YapayZeka , takım:Yapay  Zeka\n"
        "müdürlük:BTGM, T2-ARGE\n"
        "CN=t2-security,OU=Gruplar,DC=tcmb,DC=gov,DC=tr"
    )
    assert [(entry.kind, entry.name) for entry in entries] == [
        (None, "t2-arge"),
        ("group", "T2-YapayZeka"),
        ("team", "Yapay Zeka"),
        ("unit", "BTGM"),
        ("group", "t2-security"),
    ]
    assert format_entries(entries) == (
        "t2-arge, grup:T2-YapayZeka, takım:Yapay Zeka, müdürlük:BTGM, grup:t2-security"
    )
    assert group_name("CN=a\\,b,OU=x,DC=y") == "a,b"
    with pytest.raises(AccessRuleError):
        parse_entries("grup:")


def test_allow_list_admits_only_members():
    browser = rule(allow="grup:t2-arge, t2-yapayzeka")
    assert user_allowed(person(groups=["T2-ARGE"]), browser)
    # A bare name matches a team as well as a group.
    assert user_allowed(person(team="t2-yapayzeka"), browser)
    assert not user_allowed(person(team="t2-arge"), browser)  # grup: only
    assert not user_allowed(person(groups=["t2-security"]), browser)
    # Admins follow the rules for their own use too.
    assert not user_allowed(person(role=UserRole.ADMIN), browser)
    assert user_allowed(person(role=UserRole.ADMIN, groups=["t2-arge"]), browser)
    assert not user_allowed(person(role=UserRole.ADMIN, team="t2-arge"), rule(deny="t2-arge"))


def test_deny_list_wins_and_empty_rule_admits_everyone():
    terminal = rule(deny="t2-security")
    assert user_allowed(person(team="t2-arge"), terminal)
    assert not user_allowed(person(groups=["t2-security"]), terminal)
    assert not user_allowed(person(unit="T2-Security"), terminal)
    both = rule(allow="t2-arge", deny="t2-security")
    assert not user_allowed(person(team="t2-arge", groups=["t2-security"]), both)
    assert user_allowed(person(), rule())


# ---- API --------------------------------------------------------------------

async def _register(client, username):
    response = await client.post(
        "/api/auth/register",
        json={"username": username, "email": f"{username}@example.com", "password": "Password123!"},
    )
    body = response.json()
    return body["user"]["id"], {"Authorization": f"Bearer {body['access_token']}"}


async def _set(db_session, user_id, **values):
    await db_session.execute(update(User).where(User.id == user_id).values(**values))
    await db_session.commit()


async def _template_ids(client, headers):
    response = await client.get("/api/workspaces/templates", headers=headers)
    assert response.status_code == 200
    return {item["id"] for item in response.json()}


@pytest.mark.asyncio
async def test_admin_rules_control_listing_creation_and_start(client, db_session):
    admin_id, admin = await _register(client, "access-admin")
    await _set(db_session, admin_id, role=UserRole.ADMIN)
    arge_id, arge = await _register(client, "arge-user")
    await _set(db_session, arge_id, directory_groups=json.dumps(["t2-arge"]))
    security_id, security = await _register(client, "security-user")
    await _set(db_session, security_id, team="T2-Security")

    saved = await client.patch(
        "/api/admin/templates/browser-chromium",
        headers=admin,
        json={"access_allow": "grup:t2-arge, t2-yapayzeka"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["access_allow"] == "grup:t2-arge, t2-yapayzeka"
    saved = await client.patch(
        "/api/admin/templates/terminal-rocky", headers=admin, json={"access_deny": "t2-security"}
    )
    assert saved.status_code == 200, saved.text
    rejected = await client.patch(
        "/api/admin/templates/terminal-rocky", headers=admin, json={"access_deny": "grup:"}
    )
    assert rejected.status_code == 422

    assert {"browser-chromium", "terminal-rocky"} <= await _template_ids(client, arge)
    security_templates = await _template_ids(client, security)
    assert "browser-chromium" not in security_templates
    assert "terminal-rocky" not in security_templates
    assert "vscode-kilo" in security_templates
    # The admin is in neither list's teams: the allow list hides the browser.
    admin_templates = await _template_ids(client, admin)
    assert "browser-chromium" not in admin_templates
    assert "terminal-rocky" in admin_templates

    denied = await client.post(
        "/api/workspaces",
        headers=security,
        json={"name": "Kabuk", "template_id": "terminal-rocky", "flavor_id": "t1.micro"},
    )
    assert denied.status_code == 403
    created = await client.post(
        "/api/workspaces",
        headers=arge,
        json={"name": "Kabuk", "template_id": "terminal-rocky", "flavor_id": "t1.micro"},
    )
    assert created.status_code == 201, created.text
    workspace_id = created.json()["id"]
    assert (await client.post(f"/api/workspaces/{workspace_id}/stop", headers=arge)).status_code == 200

    # Withdrawing access blocks starting the workspace that already exists.
    await client.patch(
        "/api/admin/templates/terminal-rocky", headers=admin, json={"access_deny": "t2-security, t2-arge"}
    )
    blocked = await client.post(f"/api/workspaces/{workspace_id}/start", headers=arge)
    assert blocked.status_code == 403
    # Clearing the list opens the template again.
    await client.patch("/api/admin/templates/terminal-rocky", headers=admin, json={"access_deny": ""})
    started = await client.post(f"/api/workspaces/{workspace_id}/start", headers=arge)
    assert started.status_code == 200, started.text


@pytest.mark.asyncio
async def test_proxy_refuses_a_workspace_whose_template_access_was_withdrawn(client, db_session):
    from app.proxy.router import get_authorized_workspace

    admin_id, admin = await _register(client, "proxy-admin")
    await _set(db_session, admin_id, role=UserRole.ADMIN)
    owner_id, owner = await _register(client, "proxy-owner")
    created = await client.post(
        "/api/workspaces",
        headers=owner,
        json={"name": "Kabuk", "template_id": "terminal-rocky", "flavor_id": "t1.micro"},
    )
    assert created.status_code == 201, created.text
    workspace_id = created.json()["id"]
    owner_user = await db_session.get(User, owner_id)
    admin_user = await db_session.get(User, admin_id)

    assert (await get_authorized_workspace(workspace_id, db_session, owner_user)).id == workspace_id

    await _set(db_session, owner_id, team="t2-security")
    await client.patch(
        "/api/admin/templates/terminal-rocky", headers=admin, json={"access_deny": "t2-security"}
    )
    invalidate_cache()
    await db_session.refresh(owner_user)
    with pytest.raises(HTTPException) as refused:
        await get_authorized_workspace(workspace_id, db_session, owner_user)
    assert refused.value.status_code == 403
    # An admin keeps support access to another user's workspace.
    assert (await get_authorized_workspace(workspace_id, db_session, admin_user)).id == workspace_id
    assert (await db_session.execute(select(Workspace).where(Workspace.id == workspace_id))).scalar_one()


@pytest.mark.asyncio
async def test_admin_in_a_denied_team_cannot_use_the_type_for_themselves(client, db_session):
    """An admin who is a t2-arge member must not open a type that excludes t2-arge."""
    from app.proxy.router import get_authorized_workspace

    admin_id, admin = await _register(client, "arge-admin")
    await _set(db_session, admin_id, role=UserRole.ADMIN, team="T2-ARGE")
    created = await client.post(
        "/api/workspaces",
        headers=admin,
        json={"name": "Kabuk", "template_id": "terminal-rocky", "flavor_id": "t1.micro"},
    )
    assert created.status_code == 201, created.text
    workspace_id = created.json()["id"]
    assert (await client.post(f"/api/workspaces/{workspace_id}/stop", headers=admin)).status_code == 200

    saved = await client.patch(
        "/api/admin/templates/terminal-rocky", headers=admin, json={"access_deny": "t2-arge"}
    )
    assert saved.status_code == 200, saved.text
    invalidate_cache()

    assert "terminal-rocky" not in await _template_ids(client, admin)
    denied = await client.post(
        "/api/workspaces",
        headers=admin,
        json={"name": "Kabuk 2", "template_id": "terminal-rocky", "flavor_id": "t1.micro"},
    )
    assert denied.status_code == 403
    blocked = await client.post(f"/api/workspaces/{workspace_id}/start", headers=admin)
    assert blocked.status_code == 403
    admin_user = await db_session.get(User, admin_id)
    with pytest.raises(HTTPException) as refused:
        await get_authorized_workspace(workspace_id, db_session, admin_user, require_running=False)
    assert refused.value.status_code == 403

    # The admin page still lists and edits the type.
    reopened = await client.patch(
        "/api/admin/templates/terminal-rocky", headers=admin, json={"access_deny": ""}
    )
    assert reopened.status_code == 200, reopened.text
    invalidate_cache()
    assert "terminal-rocky" in await _template_ids(client, admin)


# ---- LDAP group names -----------------------------------------------------------

class FakeConnection:
    def __init__(self, nested_dns, fail=False):
        self.nested_dns = nested_dns
        self.fail = fail
        self.entries = []
        self.searches = []

    def search(self, **kwargs):
        self.searches.append(kwargs)
        if self.fail:
            raise ldap_module._ldap3()[1]("boom")
        self.entries = [SimpleNamespace(entry_dn=dn) for dn in self.nested_dns]


def test_login_records_direct_and_nested_group_names():
    config = SimpleNamespace(nested_group_search=True, user_base_dn="OU=Users,DC=tcmb,DC=gov,DC=tr")
    connection = FakeConnection(["CN=t2-yapayzeka,OU=G,DC=tcmb,DC=gov,DC=tr", "CN=T2-ARGE,OU=G,DC=tcmb,DC=gov,DC=tr"])
    names = ldap_module._group_names(
        connection,
        config,
        "CN=Ali,OU=Users,DC=tcmb,DC=gov,DC=tr",
        ["CN=T2-ARGE,OU=G,DC=tcmb,DC=gov,DC=tr"],
    )
    assert names == ("T2-ARGE", "t2-yapayzeka")
    assert connection.searches[0]["search_base"] == "DC=tcmb,DC=gov,DC=tr"
    assert "1.2.840.113556.1.4.1941" in connection.searches[0]["search_filter"]

    # A failed nested search keeps the direct groups.
    failing = FakeConnection([], fail=True)
    assert ldap_module._group_names(failing, config, "CN=Ali,DC=x", ["CN=a,OU=G,DC=x"]) == ("a",)
    # Without nested search no extra query is made.
    flat = SimpleNamespace(nested_group_search=False, user_base_dn="")
    quiet = FakeConnection(["CN=never,DC=x"])
    assert ldap_module._group_names(quiet, flat, "CN=Ali,DC=x", ["CN=a,OU=G,DC=x"]) == ("a",)
    assert quiet.searches == []
