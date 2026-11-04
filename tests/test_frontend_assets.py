from pathlib import Path

from app import __version__
from app.config import Settings


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_workspace_detail_metrics_and_tabs_have_responsive_assets():
    css = (PROJECT_ROOT / "app/static/css/kurumsal.css").read_text(encoding="utf-8")
    javascript = (PROJECT_ROOT / "app/static/js/app.js").read_text(encoding="utf-8")

    assert ".workspace-metrics-grid" in css
    assert "grid-template-columns: repeat(4, minmax(0, 1fr))" in css
    assert ".workspace-tabs-nav" in css
    assert ".tab-btn.active" in css
    assert "data-detail-metrics-ws-id" in javascript
    assert "encodeURIComponent(detailWorkspaceId)" in javascript
    assert 'btn.style.color = "var(--heading)"' in javascript


def test_app_version_cannot_be_pinned_by_stale_environment(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "0.0.0")

    assert Settings(_env_file=None).APP_VERSION == __version__


def test_unsigned_worker_ota_is_disabled_without_explicit_configuration():
    assert Settings(_env_file=None).WORKER_OTA_ALLOW_UNSIGNED is False


def test_platform_updater_waits_for_new_healthy_service():
    javascript = (PROJECT_ROOT / "app/static/js/app.js").read_text(encoding="utf-8")

    assert "waitForPlatformReady" in javascript
    assert "/api/admin/system/update-info?ts=" in javascript
    assert 'cache: "no-store"' in javascript
    assert "response.ok" in javascript
    assert "data.version" in javascript
    assert "90000" in javascript
    assert "window.location.replace" in javascript


def test_new_workspace_opens_ide_in_a_new_tab_after_successful_deployment():
    javascript = (PROJECT_ROOT / "app/static/js/app.js").read_text(encoding="utf-8")
    creation_flow = javascript.split("function initWorkspaceCreationModal()", 1)[1].split(
        "// 2. Action Buttons", 1
    )[0]

    assert 'data.type === "done"' in creation_flow
    # The AI Factory tab must stay open: the IDE never replaces it.
    assert 'window.open(ideUrl.pathname, "_blank")' in creation_flow
    assert "window.location.assign(ideUrl.pathname)" not in creation_flow
    assert "ideUrl.origin !== window.location.origin" in creation_flow


def test_admin_has_separate_worker_offline_bundle_controls():
    javascript = (PROJECT_ROOT / "app/static/js/app.js").read_text(encoding="utf-8")
    template = (PROJECT_ROOT / "app/templates/admin.html").read_text(encoding="utf-8")

    assert 'id="btn-update-worker-downloads"' in template
    assert 'id="worker-download-update-logs"' in template
    assert "/api/admin/downloads/worker/status" in javascript
    assert "/api/admin/downloads/worker/update" in javascript
    assert 'id="download-settings-form"' in template
    assert 'name="public_base_url"' in template
    assert "/api/admin/download-settings" in javascript
    assert "initDownloadSettings()" in javascript
    assert 'id="https-settings-form"' in template
    assert 'name="https_enabled"' in template
    assert 'name="http_fallback_enabled"' in template
    assert 'name="certificate"' in template
    assert 'name="private_key"' in template
    assert "/api/admin/download-settings/https" in javascript
    assert "initHttpsSettings()" in javascript


def test_admin_generates_single_use_worker_bootstrap_commands():
    javascript = (PROJECT_ROOT / "app/static/js/app.js").read_text(encoding="utf-8")
    template = (PROJECT_ROOT / "app/templates/admin.html").read_text(encoding="utf-8")

    assert 'id="worker-bootstrap-ticket-form"' in template
    assert 'id="worker-bootstrap-ticket-box"' in template
    assert "/api/admin/worker-bootstrap-tickets" in javascript
    assert "renderWorkerBootstrapTicket" in javascript
    assert "10 dakika" in template
    assert "download/install-worker.sh' | sudo bash" not in template


def test_admin_panel_exposes_category_navigation():
    template = (PROJECT_ROOT / "app/templates/admin.html").read_text(
        encoding="utf-8"
    )
    navigation = (PROJECT_ROOT / "app/templates/partials/admin_nav.html").read_text(
        encoding="utf-8"
    )

    assert 'class="admin-shell"' in template
    assert "admin_section == 'overview'" in template
    assert 'href="/admin/users"' in navigation
    assert 'href="/admin/workspaces"' in navigation
    assert 'href="/admin/workers"' in navigation
    assert 'href="/admin/integrations"' in navigation
    assert 'href="/admin/system"' in navigation
    assert 'aria-current="page"' in navigation
    assert 'data-admin-filter=".admin-user-card"' in template
    assert 'data-admin-filter="#admin-workspace-table tbody tr"' in template

    javascript = (PROJECT_ROOT / "app/static/js/app.js").read_text(
        encoding="utf-8"
    )
    assert "initAdminFilters()" in javascript


def test_admin_exposes_central_jupyter_ai_settings():
    javascript = (PROJECT_ROOT / "app/static/js/app.js").read_text(encoding="utf-8")
    template = (PROJECT_ROOT / "app/templates/admin.html").read_text(encoding="utf-8")

    assert 'id="jupyter-ai-settings-form"' in template
    assert 'name="shared_token"' in template
    assert 'data-jupyter-ai-model-row' in template
    assert 'name="gateway_model_discovery"' in template
    assert 'id="btn-test-jupyter-ai"' in template
    assert 'id="jupyter-ai-test-results"' in template
    assert "initJupyterAiSettings()" in javascript
    assert "/api/admin/jupyter-ai-settings/test" in javascript
    assert "result.workers.forEach" in javascript
    assert "CLAUDE_AVAILABLE_MODELS" not in template
    assert "syncDefaultModels" in javascript


def test_application_sources_do_not_contain_replacement_or_control_characters():
    for suffix in ("*.py", "*.html", "*.js"):
        for path in (PROJECT_ROOT / "app").rglob(suffix):
            content = path.read_text(encoding="utf-8")
            assert "\ufffd" not in content, path
            assert not any(
                ord(character) < 32 and character not in "\n\r\t"
                for character in content
            ), path


def test_workspace_flavor_picker_separates_gpu_and_uses_radio_controls():
    template = (
        PROJECT_ROOT / "app/templates/dashboard.html"
    ).read_text(encoding="utf-8")
    css = (
        PROJECT_ROOT / "app/static/css/kurumsal.css"
    ).read_text(encoding="utf-8")
    javascript = (
        PROJECT_ROOT / "app/static/js/app.js"
    ).read_text(encoding="utf-8")

    assert 'class="flavor-grid flavor-grid-cpu"' in template
    assert "flavor-card flavor-card-gpu" in template
    assert 'type="radio" name="flavor_id"' in template
    assert 'id="input-flavor-id"' not in template
    assert "available_slots" in template
    assert "eligible_accelerator_models" in template
    assert ".workspace-create-modal { max-width: 880px; }" in css
    assert ".flavor-grid-cpu" in css
    assert ".gpu-flavor-metrics" in css
    assert "syncFlavorSelection" in javascript
    assert 'input[name="flavor_id"]:checked' in javascript


def test_admin_workspace_page_exposes_persisted_flavor_toggles():
    template = (PROJECT_ROOT / "app/templates/admin.html").read_text(encoding="utf-8")
    javascript = (PROJECT_ROOT / "app/static/js/app.js").read_text(encoding="utf-8")

    assert 'id="admin-flavor-settings-table"' in template
    assert 'data-flavor-toggle="{{ flavor.id }}"' in template
    assert "initAdminFlavorSettings();" in javascript
    assert "/api/admin/flavors/" in javascript
    assert 'id="admin-flavor-create-form"' in template
    assert 'id="btn-sync-workspace-catalog"' in template
    assert 'id="admin-custom-template-table"' in template
    assert "/api/admin/catalog/sync" in javascript


def test_dense_data_views_have_responsive_overflow_guards():
    base_css = (PROJECT_ROOT / "app/static/css/style.css").read_text(
        encoding="utf-8"
    )
    corporate_css = (PROJECT_ROOT / "app/static/css/kurumsal.css").read_text(
        encoding="utf-8"
    )
    admin = (PROJECT_ROOT / "app/templates/admin.html").read_text(encoding="utf-8")
    images = (
        PROJECT_ROOT / "app/templates/partials/admin_images.html"
    ).read_text(encoding="utf-8")
    javascript = (PROJECT_ROOT / "app/static/js/app.js").read_text(
        encoding="utf-8"
    )

    assert ".data-table-shell" in base_css
    assert "overflow-x: auto" in base_css
    assert ".responsive-card-table td::before" in corporate_css
    assert "content: attr(data-label)" in corporate_css
    assert ".worker-sync-item" in corporate_css
    assert 'class="data-table-shell' in admin
    assert 'class="table responsive-card-table" id="admin-workspace-table"' in admin
    assert 'data-label="Worker"' in admin
    assert "responsive-card-table-shell image-catalog-shell" in images
    assert 'data-label="Digest / SHA-256"' in javascript
    assert 'class="worker-sync-item"' in javascript


def test_worker_inventory_fits_admin_width_and_collapses_to_cards():
    css = (PROJECT_ROOT / "app/static/css/kurumsal.css").read_text(
        encoding="utf-8"
    )
    template = (PROJECT_ROOT / "app/templates/admin.html").read_text(
        encoding="utf-8"
    )

    assert "responsive-card-table-shell worker-table-shell" in template
    assert 'class="table responsive-card-table" id="admin-nodes-table"' in template
    for label in (
        "Worker",
        "Durum",
        "CPU",
        "RAM",
        "GPU",
        "Disk / Container",
        "Etiketler",
        "İşlemler",
    ):
        assert f'data-label="{label}"' in template
    assert "#admin-nodes-table {" in css
    assert "table-layout: fixed" in css
    assert "#admin-nodes-table { min-width: 1050px; }" not in css
    assert "@media (max-width: 1180px)" in css
    assert ".worker-table-shell" in css
    assert "content: attr(data-label)" in css


def test_platform_update_has_clear_methods_release_summary_and_live_log():
    css = (PROJECT_ROOT / "app/static/css/kurumsal.css").read_text(
        encoding="utf-8"
    )
    template = (PROJECT_ROOT / "app/templates/admin.html").read_text(
        encoding="utf-8"
    )
    javascript = (PROJECT_ROOT / "app/static/js/app.js").read_text(
        encoding="utf-8"
    )

    assert 'class="card platform-update-card"' in template
    assert 'class="platform-current-release"' in template
    assert "platform-update-option--primary" in template
    assert "Güncellemeyi Kontrol Et" in template
    assert "Güncellemeyi Yükle" in template
    assert 'id="platform-installed-version"' in template
    assert 'id="platform-published-version"' in template
    assert "Yerel platform bundle" in template
    assert 'class="platform-update-log"' in template
    assert 'aria-live="polite"' in template
    assert ".platform-update-options" in css
    assert ".platform-git-fields" in css
    assert ".platform-update-log" in css
    assert template.count('name="allow_unsigned"') == 2
    assert template.count('class="unsigned-update-control"') == 2
    assert 'value="https://github.com/aydinguven/devcloud.git"' in template
    assert 'name="allow_unsigned" type="checkbox" value="true" checked' not in template
    assert ".unsigned-update-control:has(input:checked)" in css
    assert 'form.elements.namedItem("allow_unsigned")' in javascript
    assert "Release imzası doğrulanmayacak" in javascript
    assert '"badge-error"' in javascript
    assert '"Kuyruğa alınıyor..."' in javascript
    assert "/api/admin/system/release-check" in javascript
    assert "Controller güncelleme için yeniden başlatılıyor" in javascript


def test_worker_inventory_shows_version_and_live_upgrade_state():
    css = (PROJECT_ROOT / "app/static/css/kurumsal.css").read_text(
        encoding="utf-8"
    )
    template = (PROJECT_ROOT / "app/templates/admin.html").read_text(
        encoding="utf-8"
    )
    javascript = (PROJECT_ROOT / "app/static/js/app.js").read_text(
        encoding="utf-8"
    )

    assert 'class="node-version badge badge-neutral"' in template
    assert "node-upgrade-state badge" in template
    assert "upgrade_status.get('target_version')" in template
    assert "renderWorkerUpgradeState" in javascript
    assert "d.agent_version" in javascript
    assert "d.upgrade_status" in javascript
    assert "Object.keys(d.upgrade_status).length" in javascript
    assert ".node-release-line" in css
    assert 'class="node-upgrade-detail' in template
    assert "upgrade.message" in javascript
    assert 'result.status === "already_current"' in javascript
    assert "/upgrade-check" in javascript
    assert 'button.textContent = "Güncelle"' in javascript
    assert ".node-upgrade-detail.is-error" in css


def test_worker_inventory_shows_live_gpu_runtime_state():
    template = (PROJECT_ROOT / "app/templates/admin.html").read_text(
        encoding="utf-8"
    )
    javascript = (PROJECT_ROOT / "app/static/js/app.js").read_text(
        encoding="utf-8"
    )

    assert 'class="node-gpu-cell"' in template
    assert "accelerator_runtime" in template
    assert "GPU Hatası" in template
    assert "renderWorkerGpu" in javascript
    assert "d.accelerators" in javascript
    assert "memory_used_mb" in javascript
    assert ".node-gpu-detail" in (
        PROJECT_ROOT / "app/static/css/kurumsal.css"
    ).read_text(encoding="utf-8")


def test_mlflow_server_is_admin_managed_and_credentials_are_per_user():
    template = (PROJECT_ROOT / "app/templates/models.html").read_text(
        encoding="utf-8"
    )
    admin_template = (PROJECT_ROOT / "app/templates/admin.html").read_text(
        encoding="utf-8"
    )
    javascript = (PROJECT_ROOT / "app/static/js/app.js").read_text(
        encoding="utf-8"
    )

    assert 'id="mlflow-settings-form"' in template
    assert "Sunucu adresi yönetici tarafından belirlenir" in template
    assert "model eğitmez, çalıştırmaz veya değiştirmez" in template
    assert 'name="base_url"' not in template
    assert 'id="admin-mlflow-settings-form"' in admin_template
    assert 'fetch("/api/admin/mlflow-server-settings"' in javascript
    assert 'send("/api/mlflow/settings/test", "POST")' in javascript
    assert 'send("/api/mlflow/settings", "PUT")' in javascript


def test_mlflow_tracking_pages_cover_runs_artifacts_comparison_and_lineage():
    experiments = (PROJECT_ROOT / "app/templates/experiments.html").read_text(encoding="utf-8")
    detail = (PROJECT_ROOT / "app/templates/experiment_detail.html").read_text(encoding="utf-8")
    run = (PROJECT_ROOT / "app/templates/run_detail.html").read_text(encoding="utf-8")
    compare = (PROJECT_ROOT / "app/templates/run_compare.html").read_text(encoding="utf-8")
    nav = (PROJECT_ROOT / "app/templates/partials/mlflow_nav.html").read_text(encoding="utf-8")
    javascript = (PROJECT_ROOT / "app/static/js/mlflow.js").read_text(encoding="utf-8")
    dashboard = (PROJECT_ROOT / "app/templates/mlflow_dashboard.html").read_text(encoding="utf-8")

    assert "Deneyler ve Run'lar" in experiments
    assert "filter_string" in detail
    assert "data-run-select" in javascript
    assert "Artifact tarayıcısı" in run
    assert "Model Soy Ağacı" in run
    assert "compare-metric-chart" in compare
    assert "/api/mlflow/overview" in javascript
    assert "/history" in javascript
    assert "/artifacts/preview" in javascript
    assert "devcloud.mlflow.savedFilters.v1" in javascript
    assert 'id="mlflow-dashboard"' in dashboard
    assert 'href="/mlflow"' in nav
    assert 'href="/experiments"' in nav
    assert 'href="/models"' in nav


def test_worker_bootstrap_uses_one_time_admin_command():
    template = (PROJECT_ROOT / "app/templates/downloads.html").read_text(
        encoding="utf-8"
    )
    bootstrap = (PROJECT_ROOT / "app/templates/install_worker.sh").read_text(
        encoding="utf-8"
    )

    assert "Admin &gt; Worker Node'ları" in template
    assert "worker_bootstrap_url" not in template
    assert "devcloud-setup.sh" in bootstrap
    assert "--yes install worker" in bootstrap
    assert "sha256sum -c" in bootstrap
    assert 'tar -xf "${BUNDLE_PATH}"' in bootstrap
    assert "devcloud-offline-*.tar.gz.sha256" in template
    assert "devcloud-worker-offline-*.tar.gz.sha256" in template
    assert "Worker name" in bootstrap
    assert "/api/bootstrap/workers/" not in bootstrap
    assert "DEVCLOUD_NODE_TOKEN" not in template


def test_institutional_brand_mark_replaces_text_placeholder():
    template = (PROJECT_ROOT / "app/templates/base.html").read_text(encoding="utf-8")
    css = (PROJECT_ROOT / "app/static/css/kurumsal.css").read_text(encoding="utf-8")
    logo_svg = (PROJECT_ROOT / "app/static/img/tcmb_ai_factory_logo.svg").read_text(encoding="utf-8")
    favicon_svg = (PROJECT_ROOT / "app/static/favicon.svg").read_text(encoding="utf-8")

    assert 'TCMB' in template
    assert 'AI FACTORY' in template
    assert 'Yapay Zeka Geliştirme Platformu' in template
    assert 'Yapay Zekâ' not in template
    assert 'logo-icon" aria-hidden="true">DC' not in template
    assert ".brand-tcmb" in css
    assert ".brand-ai" in css
    assert 'TCMB' in logo_svg
    assert 'AI FACTORY' in logo_svg
    assert 'Yapay Zeka Geliştirme Platformu' in logo_svg
    assert 'Yapay Zekâ' not in logo_svg
    assert '<ellipse' in favicon_svg
    assert '#d50032' in favicon_svg
    assert '#263244' in favicon_svg


def test_linear_onboarding_tour_has_multiple_persistent_steps():
    base = (PROJECT_ROOT / "app/templates/base.html").read_text(encoding="utf-8")
    dashboard = (PROJECT_ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
    workspace = (PROJECT_ROOT / "app/templates/workspace_detail.html").read_text(encoding="utf-8")
    profile = (PROJECT_ROOT / "app/templates/profile.html").read_text(encoding="utf-8")
    models = (PROJECT_ROOT / "app/templates/models.html").read_text(encoding="utf-8")
    mlflow_nav = (
        PROJECT_ROOT / "app/templates/partials/mlflow_nav.html"
    ).read_text(encoding="utf-8")
    admin = (PROJECT_ROOT / "app/templates/admin.html").read_text(encoding="utf-8")
    app_javascript = (PROJECT_ROOT / "app/static/js/app.js").read_text(
        encoding="utf-8"
    )
    javascript = (PROJECT_ROOT / "app/static/js/onboarding-tour.js").read_text(
        encoding="utf-8"
    )
    css = (PROJECT_ROOT / "app/static/css/onboarding-tour.css").read_text(
        encoding="utf-8"
    )

    assert "onboarding-tour.css" in base
    assert "onboarding-tour.js" in base
    assert "data-onboarding-restart" in base
    for target in (
        "workspace-create-button",
        "workspace-name",
        "template-picker",
        "flavor-picker",
        "workspace-create-submit",
        "quota-summary",
        "workspace-demo-overview",
        "workspace-demo-metrics",
        "workspace-demo-logs",
        "workspace-demo-files",
        "workspace-demo-ports",
    ):
        assert f'data-tour="{target}"' in dashboard
    for target in (
        "workspace-actions",
        "workspace-metrics",
        "workspace-tab-logs",
        "workspace-tab-files",
        "workspace-tab-ports",
    ):
        assert f'data-tour="{target}"' in workspace
    assert 'data-tour="profile-details"' in profile
    assert 'data-tour="tour-restart-profile"' in profile
    assert 'data-tour="mlflow-personal-settings"' in models
    assert 'data-tour="mlflow-nav"' in mlflow_nav
    assert 'data-tour="admin-summary"' in admin
    assert 'data-tour="admin-overview"' in admin

    stable_steps = (
        "ws-create-open",
        "ws-create-dialog",
        "ws-create-name",
        "ws-create-template",
        "ws-create-flavor",
        "ws-create-submit",
        "dashboard-quota",
        "ws-detail-overview",
        "ws-detail-metrics",
        "ws-detail-logs",
        "ws-detail-files",
        "ws-detail-ports",
        "mlflow-nav",
        "mlflow-connection",
        "profile-details",
        "profile-tour-control",
        "admin-summary",
        "admin-sections",
    )
    for step in stable_steps:
        assert f'id: "{step}"' in javascript

    assert 'next.textContent = adjacentStep(step, 1) ? "Devam"' in javascript
    assert "Bölümünü Atla" in javascript
    assert "showTopicChoice" not in javascript
    assert 'label: "Göster"' not in javascript
    assert 'label: "Atla"' not in javascript
    assert "showWelcome" not in javascript
    assert 'current_step: step.id' in javascript
    assert 'state.status === "not_started" && state.auto_offer' in javascript
    assert "await startTour()" in javascript
    assert 'window.location.assign(route)' in javascript
    assert "DevCloudWorkspaceCreateModal" in javascript
    assert "DevCloudWorkspaceCreateModal" in app_javascript
    assert 'close({force: true})' in javascript
    assert "handleModalKeydown" in app_javascript
    assert 'event.key === "Escape"' in app_javascript
    assert "setBackgroundInert" in app_javascript
    assert "restoreBackground" in app_javascript
    assert "previousFocus" in app_javascript
    assert "LEGACY_STEP" in javascript
    assert "onboardingConflict" in javascript
    assert 'event.key === "Escape"' in javascript
    assert "trapFocus" in javascript
    assert "makeBackgroundInert" in javascript
    assert 'callout.setAttribute("aria-labelledby"' in javascript
    assert "ResizeObserver" in javascript
    assert "/api/onboarding/state" in javascript
    assert "/api/onboarding/restart" in javascript
    assert "prefers-reduced-motion" in css
    assert ".onboarding-tour-scrim" in css
    assert ".onboarding-demo-workspace" in css
    assert "pointer-events: none !important" in css
    assert "http://" not in javascript
    assert "https://" not in javascript



def test_admin_catalog_fields_use_inline_edit_and_image_cards_align():
    admin = (PROJECT_ROOT / "app/templates/admin.html").read_text(encoding="utf-8")
    images = (
        PROJECT_ROOT / "app/templates/partials/admin_images.html"
    ).read_text(encoding="utf-8")
    javascript = (PROJECT_ROOT / "app/static/js/app.js").read_text(
        encoding="utf-8"
    )
    css = (PROJECT_ROOT / "app/static/css/kurumsal.css").read_text(
        encoding="utf-8"
    )

    assert 'id="jupyter-ai-model-list" data-inline-edit-catalog' in admin
    assert 'id="admin-custom-template-table"' in admin
    assert 'data-inline-edit-catalog' in admin
    assert 'data-inline-edit-input' in admin
    assert 'data-inline-edit-label' in admin
    assert "initInlineEditControls()" in javascript
    assert 'input.readOnly = true' in javascript
    assert 'toggle.textContent = editing ? "Bitti" : "Düzenle"' in javascript
    assert "closeInlineEditControls(modelList)" in javascript
    assert "closeInlineEditControls(row)" in javascript
    assert ".inline-edit-control" in css
    assert ".inline-edit-toggle" in css

    assert "grid grid-cols-2 image-import-grid" in images
    assert images.count("card image-import-card") == 2
    assert images.count("image-import-form") == 2
    assert images.count("image-import-actions") == 2
    assert 'role="status" aria-live="polite"' in images
    assert ".image-import-grid" in css
    assert ".image-import-card" in css
    assert ".image-import-form" in css
    assert ".image-import-actions" in css
    assert "align-items: stretch" in css
    assert "margin-top: auto" in css
