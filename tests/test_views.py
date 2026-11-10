import pytest
from httpx import AsyncClient


async def get_authenticated_headers(client: AsyncClient, username: str = "dev_user") -> dict[str, str]:
    """Helper to register and return authorization headers."""
    resp = await client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "Password123!",
            "full_name": "Dev User",
        },
    )
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_view_routes_render_html(client: AsyncClient):
    """Test that HTML view routes render without 500 errors."""
    # 1. Login page
    login_resp = await client.get("/login")
    assert login_resp.status_code == 200
    assert "AI Factory'ye Hoş Geldiniz" in login_resp.text
    assert "Yeni Hesap Oluştur" in login_resp.text
    from app.config import settings
    assert '<html lang="tr">' in login_resp.text
    assert f"v{settings.APP_VERSION}" in login_resp.text
    assert f"/static/css/kurumsal.css?v={settings.APP_VERSION}-" in login_resp.text
    assert 'href="https://git.aydin.cloud/aydin/aifactory"' not in login_resp.text
    assert "Git Deposu" not in login_resp.text

    # 2. Register page
    reg_resp = await client.get("/register")
    assert reg_resp.status_code == 200
    assert "Hesabınızı Oluşturun" in reg_resp.text
    assert "1 CPU, 1 GB RAM ve 10 GB Disk" in reg_resp.text

    # 3. Root redirect to /login for unauthenticated users
    root_resp = await client.get("/", follow_redirects=False)
    assert root_resp.status_code == 302
    assert root_resp.headers["location"] == "/login"

    # 4. Authenticated dashboard rendering
    headers = await get_authenticated_headers(client, "dashboard_view_user")
    auth_dashboard = await client.get("/", headers=headers)
    assert auth_dashboard.status_code == 200
    assert "Çalışma Alanlarım" in auth_dashboard.text
    assert "Toplam Sistem Kullanımı" in auth_dashboard.text
    assert "Kota Kullanımı" in auth_dashboard.text
    assert "Kaynak Profili" in auth_dashboard.text
    assert "CPU ve Bellek" in auth_dashboard.text
    assert "GPU Hızlandırma" in auth_dashboard.text
    assert 'type="radio" name="flavor_id"' in auth_dashboard.text
    assert 'class="selectable-card flavor-card flavor-card-gpu is-unavailable"' in (
        auth_dashboard.text
    )
    assert "GPU kotanız yok" in auth_dashboard.text
    assert "CPU" in auth_dashboard.text
    assert "RAM" in auth_dashboard.text
    for template_name in (
        "Kilo Code", "Jupyter Notebook", "Terminal (Rocky Linux 10)"
    ):
        assert template_name in auth_dashboard.text
    assert 'class="template-card-title"' in auth_dashboard.text
    assert 'type="radio" name="template_id" value="vscode-kilo"' in auth_dashboard.text
    assert 'id="workspace-search"' not in auth_dashboard.text

    # 5. Create a workspace and render dashboard + detail page
    create_payload = {
        "name": "Dashboard Template Test",
        "description": "Checking template render",
        "template_id": "vscode-kilo",
        "flavor_id": "t1.nano",
    }
    ws_res = await client.post("/api/workspaces", json=create_payload, headers=headers)
    assert ws_res.status_code == 201
    ws_id = ws_res.json()["id"]

    # Render dashboard with existing workspace
    auth_dashboard_with_ws = await client.get("/", headers=headers)
    assert auth_dashboard_with_ws.status_code == 200
    assert "Dashboard Template Test" in auth_dashboard_with_ws.text
    assert 'aria-controls="workspace-results"' in auth_dashboard_with_ws.text
    assert 'data-workspace-status="running"' in auth_dashboard_with_ws.text
    assert 'data-workspace-search="Dashboard Template Test Checking template render vscode-kilo t1.nano ' in auth_dashboard_with_ws.text

    usage_resp = await client.get("/api/workspaces/usage", headers=headers)
    assert usage_resp.status_code == 200
    usage = usage_resp.json()
    assert set(usage["system"]) == {"cpu", "memory", "disk"}
    assert usage["user"]["cpu"]["used"] == 0.5
    assert usage["user"]["memory"]["used_display"] == "512.0 MB"
    assert usage["user"]["cpu"]["remaining"] == 0.5

    # Render workspace detail page
    detail_resp = await client.get(f"/workspaces/{ws_id}", headers=headers)
    assert detail_resp.status_code == 200
    assert "Dashboard Template Test" in detail_resp.text
    assert f'data-detail-metrics-ws-id="{ws_id}"' in detail_resp.text
    assert 'class="workspace-metrics-grid"' in detail_resp.text
    assert 'class="workspace-tabs-nav"' in detail_resp.text
