import pytest
from httpx import AsyncClient
from sqlalchemy import select, update

from app.auth.ldap import (
    DirectoryIdentity,
    decrypt_directory_secret,
    encrypt_directory_secret,
)
from app.models.directory_settings import DirectorySettings
from app.models.user import User, UserRole
from tests.conftest import TestingSessionLocal


async def _admin_headers(client: AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/auth/register",
        json={
            "username": "directory_admin",
            "email": "directory-admin@example.com",
            "password": "AdminPassword123!",
        },
    )
    user_id = response.json()["user"]["id"]
    async with TestingSessionLocal() as session:
        await session.execute(
            update(User).where(User.id == user_id).values(role=UserRole.ADMIN)
        )
        await session.commit()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _settings_payload(**overrides):
    payload = {
        "enabled": True,
        "server_host": "ldaps.tcmb.gov.tr",
        "server_port": 686,
        "use_ssl": True,
        "validate_tls": True,
        "ca_cert_file": "",
        "connect_timeout_seconds": 10,
        "bind_dn": "CN=svc-devcloud,OU=Services,DC=example,DC=com",
        "bind_password": "bind-secret",
        "user_base_dn": "OU=Users,DC=example,DC=com",
        "user_filter": "(&(objectClass=user)(sAMAccountName={username}))",
        "username_attribute": "sAMAccountName",
        "email_attribute": "mail",
        "display_name_attribute": "displayName",
        "team_attribute": "department",
        "directorate_attribute": "division",
        "group_membership_attribute": "memberOf",
        "required_group_dn": "CN=AIFactory,OU=Groups,DC=example,DC=com",
        "admin_group_dn": "CN=AIFactory-Admins,OU=Groups,DC=example,DC=com",
        "nested_group_search": True,
    }
    payload.update(overrides)
    return payload


def test_directory_secret_encryption_round_trip():
    encrypted = encrypt_directory_secret("very-secret-password")
    assert encrypted != "very-secret-password"
    assert decrypt_directory_secret(encrypted) == "very-secret-password"


@pytest.mark.asyncio
async def test_admin_can_save_directory_settings_without_password_disclosure(
    client: AsyncClient,
):
    headers = await _admin_headers(client)
    response = await client.put(
        "/api/admin/directory-settings",
        headers=headers,
        json=_settings_payload(),
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["server_host"] == "ldaps.tcmb.gov.tr"
    assert data["server_port"] == 686
    assert data["has_bind_password"] is True
    assert data["team_attribute"] == "department"
    assert data["directorate_attribute"] == "division"
    assert "bind_password" not in data

    async with TestingSessionLocal() as session:
        record = await session.get(DirectorySettings, 1)
        assert record.encrypted_bind_password != "bind-secret"
        original_ciphertext = record.encrypted_bind_password

    payload = _settings_payload(bind_password=None, connect_timeout_seconds=15)
    second = await client.put(
        "/api/admin/directory-settings", headers=headers, json=payload
    )
    assert second.status_code == 200, second.text
    async with TestingSessionLocal() as session:
        record = await session.get(DirectorySettings, 1)
        assert record.encrypted_bind_password == original_ciphertext
        assert record.connect_timeout_seconds == 15


@pytest.mark.asyncio
async def test_directory_connection_test_uses_unsaved_form_values(
    client: AsyncClient, monkeypatch
):
    headers = await _admin_headers(client)

    def fake_test(config):
        assert config.server_host == "ldaps.tcmb.gov.tr"
        assert config.server_port == 686
        assert config.bind_password == "bind-secret"
        return "Bind ve kullanıcı tabanı araması başarılı.", 27

    monkeypatch.setattr(
        "app.routes.admin.users.test_directory_configuration", fake_test
    )
    response = await client.post(
        "/api/admin/directory-settings/test",
        headers=headers,
        json=_settings_payload(),
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "success": True,
        "message": "Bind ve kullanıcı tabanı araması başarılı.",
        "server": "ldaps://ldaps.tcmb.gov.tr:686",
        "response_time_ms": 27,
    }


@pytest.mark.asyncio
async def test_successful_directory_login_provisions_admin_user(
    client: AsyncClient, monkeypatch
):
    async with TestingSessionLocal() as session:
        session.add(
            DirectorySettings(
                id=1,
                enabled=True,
                server_host="ldaps.tcmb.gov.tr",
                server_port=686,
                bind_dn="CN=svc,DC=example,DC=com",
                encrypted_bind_password=encrypt_directory_secret("bind-secret"),
                user_base_dn="OU=Users,DC=example,DC=com",
                required_group_dn="CN=AIFactory,DC=example,DC=com",
                admin_group_dn="CN=AIFactory-Admins,DC=example,DC=com",
            )
        )
        await session.commit()

    def fake_authenticate(config, username, password):
        assert username == "aydin"
        assert password == "user-secret"
        return DirectoryIdentity(
            username="aydin",
            email="aydin@example.com",
            full_name="Aydin Example",
            team="AI Platform",
            directorate="Data Technologies",
            user_dn="CN=Aydin,OU=Users,DC=example,DC=com",
            groups=("CN=AIFactory-Admins,DC=example,DC=com",),
            is_admin=True,
        )

    monkeypatch.setattr(
        "app.auth.ldap.authenticate_directory_user", fake_authenticate
    )
    response = await client.post(
        "/api/auth/login",
        json={"username": "aydin", "password": "user-secret"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["user"]["auth_source"] == "active_directory"
    assert response.json()["user"]["role"] == "admin"

    async with TestingSessionLocal() as session:
        user = (
            await session.execute(
                select(User).where(User.username == "aydin")
            )
        ).scalar_one()
        assert user.role == UserRole.ADMIN
        assert user.auth_source == "active_directory"
        assert user.team == "AI Platform"
        assert user.directorate == "Data Technologies"


@pytest.mark.asyncio
async def test_enabled_directory_disables_public_registration(client: AsyncClient):
    async with TestingSessionLocal() as session:
        session.add(DirectorySettings(id=1, enabled=True))
        await session.commit()

    api_response = await client.post(
        "/api/auth/register",
        json={
            "username": "bypass_user",
            "email": "bypass@example.com",
            "password": "Password123!",
        },
    )
    assert api_response.status_code == 403

    login_page = await client.get("/login")
    assert login_page.status_code == 200
    assert "Yeni Hesap Oluştur" not in login_page.text
    assert "Kurumsal dizin hesabınızla giriş yapın" in login_page.text

    register_page = await client.get("/register")
    assert register_page.status_code == 302
    assert register_page.headers["location"] == "/login"


def test_organization_unit_is_derived_from_the_manager_chain():
    from app.auth.ldap import (
        DirectoryPerson,
        derive_organization_unit,
        parse_unit_head_titles,
    )

    def person(dn, team, title, manager="", division="BİLGİ TEKNOLOJİLERİ"):
        return DirectoryPerson(dn=dn, team=team, title=title, directorate=division, manager_dn=manager)

    people = {
        p.dn: p
        for p in (
            person("CN=K016972", "BAŞKANLIK", "MÜDÜR", division="BAŞKANLIK"),
            person("CN=K012950", "BİLGİ TEKNOLOJİLERİ", "GENEL MÜDÜR", "CN=K016972"),
            person("CN=K014810", "YENİLİKÇİ TEKNOLOJİLER", "MÜDÜR", "CN=K012950"),
            person("CN=LEAD", "ARAŞTIRMA VE GELİŞTİRME", "UZMAN", "CN=K014810"),
            person("CN=LOOP-A", "X", "UZMAN", "CN=LOOP-B"),
            person("CN=LOOP-B", "X", "UZMAN", "CN=LOOP-A"),
        )
    }
    heads = parse_unit_head_titles(" Müdür , ")

    def derive(p):
        return derive_organization_unit(p, people.get, heads)

    # Direct report and a report through an in-team lead reach the same MÜDÜR.
    assert derive(person("CN=K015570", "ARAŞTIRMA VE GELİŞTİRME", "BİLİŞİM UZMANI", "CN=K014810")) == "YENİLİKÇİ TEKNOLOJİLER"
    assert derive(person("CN=NEW", "ARAŞTIRMA VE GELİŞTİRME", "UZMAN", "CN=LEAD")) == "YENİLİKÇİ TEKNOLOJİLER"
    # A MÜDÜR's müdürlük is their own team.
    assert derive(people["CN=K014810"]) == "YENİLİKÇİ TEKNOLOJİLER"
    # The walk never crosses into another division, missing managers or loops.
    assert derive(people["CN=K012950"]) == ""
    assert derive(person("CN=ORPHAN", "Y", "UZMAN", "CN=GONE")) == ""
    assert derive(people["CN=LOOP-A"]) == ""


@pytest.mark.asyncio
async def test_directory_sync_places_teams_and_refreshes_users(client: AsyncClient, monkeypatch):
    from app.directory_sync import DirectoryEntry
    from app.models.directory_team import DirectoryTeam

    headers = await _admin_headers(client)
    assert (await client.put("/api/admin/directory-settings", headers=headers, json=_settings_payload())).status_code == 200

    bt, yt, arge = "BİLGİ TEKNOLOJİLERİ", "YENİLİKÇİ TEKNOLOJİLER", "ARAŞTIRMA VE GELİŞTİRME"

    def entry(username, name, team, title, manager="", division=bt, in_scope=True):
        return DirectoryEntry(
            dn=f"CN={username},CN=Users", username=username, full_name=name, team=team,
            title=title, directorate=division, manager_dn=f"CN={manager},CN=Users" if manager else "",
            in_scope=in_scope,
        )

    entries = [
        entry("K012950", "Mehmet Zahit Ateş", bt, "GENEL MÜDÜR"),
        entry("K012951", "BT Uzmanı", bt, "UZMAN", "K012950"),
        entry("K014810", "Kemal Özgür Duman", yt, "MÜDÜR", "K012950"),
        entry("K015570", "Aydın Güven Aslangören", arge, "BİLİŞİM UZMANI", "K014810"),
        entry("K015571", "Elif Yılmaz", arge, "UZMAN", "K999999"),  # stale manager DN
        entry("K017000", "Arşiv Uzmanı", "ARŞİV", "UZMAN"),  # no manager
        # The müdür is outside the sync search and has no division in AD;
        # the chain still reaches him.
        entry("K016408", "Seda Gökhüseyin", "İŞLETİM SİSTEMİ YÖNETİMİ", "UZMAN", "K016400"),
        entry("K016400", "Burak Çelik", "ALTYAPI YÖNETİMİ", "MÜDÜR", "K012950", division="", in_scope=False),
    ]
    monkeypatch.setattr("app.directory_sync.fetch_directory_entries", lambda config: entries)

    async with TestingSessionLocal() as session:
        for username, team in (("K015571", "ESKİ TAKIM"), ("K014810", yt), ("gone", "X")):
            session.add(User(username=username, email=f"{username}@x", hashed_password="x",
                             team=team, auth_source="active_directory"))
        await session.commit()

    response = await client.post("/api/admin/directory-sync", headers=headers)
    assert response.status_code == 200, response.text
    summary = response.json()
    # The out-of-scope müdür completes chains but is not counted or placed.
    assert (summary["ad_people"], summary["teams"], summary["units"], summary["division_teams"]) == (7, 5, 2, 1)
    assert summary["updated_users"] == 2 and summary["missing_users"] == ["gone"]
    assert summary["unassigned_teams"] == [{"name": "ARŞİV", "reason": "no_manager", "members": 1}]

    async with TestingSessionLocal() as session:
        users = {u.username: u for u in (await session.execute(select(User))).scalars()}
        teams = {t.name: t for t in (await session.execute(select(DirectoryTeam))).scalars()}
    # Only existing directory users are refreshed; nobody is created.
    assert set(users) == {"directory_admin", "K015571", "K014810", "gone"}
    # A broken personal chain falls back to the team's müdürlük.
    elif_ = users["K015571"]
    assert (elif_.team, elif_.organization_unit, elif_.full_name) == (arge, yt, "Elif Yılmaz")
    assert users["K014810"].managed_unit == yt
    assert (teams[arge].status, teams[arge].organization_unit, teams[arge].head_name) == ("team", yt, "Kemal Özgür Duman")
    assert (teams[bt].status, teams[bt].head_name) == ("division", "Mehmet Zahit Ateş")
    os_team = teams["İŞLETİM SİSTEMİ YÖNETİMİ"]
    assert (os_team.status, os_team.organization_unit, os_team.head_name) == ("team", "ALTYAPI YÖNETİMİ", "Burak Çelik")
    assert "ALTYAPI YÖNETİMİ" not in teams
