(() => {
  "use strict";

  const esc = value => String(value ?? "").replace(/[&<>'"]/g, character => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[character]);
  const request = async (url, options = {}) => {
    const response = await fetch(url, {cache: "no-store", headers: {"Content-Type": "application/json"}, ...options});
    const data = response.status === 204 ? {} : await response.json().catch(() => ({}));
    if (!response.ok) {
      let detail = data.detail || `İstek başarısız (${response.status})`;
      if (Array.isArray(detail)) detail = detail.map(item => item.msg).join("; ");
      throw new Error(detail);
    }
    return data;
  };
  const setStatus = (element, message, kind = "") => {
    element.textContent = message;
    element.className = `quota-form-status${kind ? ` quota-status-${kind}` : ""}`;
  };

  function initDeployRegistry() {
    const form = document.getElementById("deploy-registry-settings-form");
    if (!form) return;
    const status = document.getElementById("deploy-registry-form-status");
    const badge = document.getElementById("deploy-registry-status-badge");
    const saveButton = document.getElementById("btn-save-deploy-registry");
    const testButton = document.getElementById("btn-test-deploy-registry");
    const payload = () => {
      const password = String(form.elements.password.value || "");
      return {
        enabled: form.elements.enabled.checked,
        registry_url: String(form.elements.registry_url.value || "").trim(),
        username: String(form.elements.username.value || "").trim(),
        password: form.elements.clear_password.checked ? "" : (password || null),
        tls_verify: form.elements.tls_verify.checked,
        manifest_format: form.elements.manifest_format.value,
      };
    };

    form.addEventListener("submit", async event => {
      event.preventDefault();
      saveButton.disabled = true;
      setStatus(status, "Kaydediliyor...");
      try {
        const saved = await request("/api/admin/deploy-registry-settings", {method: "PUT", body: JSON.stringify(payload())});
        form.elements.password.value = "";
        form.elements.clear_password.checked = false;
        form.elements.password.placeholder = saved.has_password ? "Kayıtlı parolayı korumak için boş bırakın" : "Registry parolasını girin";
        badge.className = `badge ${saved.enabled ? "badge-running" : "badge-stopped"}`;
        badge.textContent = saved.enabled ? "Etkin" : "Devre Dışı";
        setStatus(status, "Dağıtım registry ayarları şifreli olarak kaydedildi.", "success");
      } catch (error) {
        setStatus(status, error.message, "error");
      } finally {
        saveButton.disabled = false;
      }
    });

    testButton.addEventListener("click", async () => {
      testButton.disabled = true;
      setStatus(status, "Registry controller'dan test ediliyor...");
      const candidate = payload();
      try {
        const result = await request("/api/admin/deploy-registry-settings/test", {
          method: "POST",
          body: JSON.stringify({registry_url: candidate.registry_url, username: candidate.username, password: candidate.password, tls_verify: candidate.tls_verify}),
        });
        const latency = Number.isInteger(result.latency_ms) ? ` · ${result.latency_ms} ms` : "";
        setStatus(status, `${result.ok ? "✓" : "✗"} ${result.message}${latency}`, result.ok ? "success" : "error");
      } catch (error) {
        setStatus(status, error.message, "error");
      } finally {
        testButton.disabled = false;
      }
    });
  }

  // Admins only keep the RFS codes; applications are created by their first
  // release from the MLflow deploy modal and can be disabled, rebound or deleted here.
  function initProjects() {
    const form = document.getElementById("k8s-project-form");
    const list = document.getElementById("k8s-project-list");
    if (!form || !list) return;
    const status = document.getElementById("k8s-project-form-status");
    const saveButton = document.getElementById("btn-save-k8s-project");
    const cancelButton = document.getElementById("btn-cancel-k8s-project");
    let projects = [];
    let editing = null;

    // PUT replaces every field, so unchanged ones are sent back as loaded.
    const projectBody = (project, changes = {}) => ({
      rfs_code: project.rfs_code,
      name: project.name,
      namespace: project.namespace,
      description: project.description,
      enabled: project.enabled,
      ...changes,
    });
    const applicationBody = (application, changes = {}) => ({
      app_name: application.app_name,
      mlflow_model_name: application.mlflow_model_name,
      bitbucket_url: application.bitbucket_url,
      description: application.description,
      enabled: application.enabled,
      ...changes,
    });
    const stateBadge = enabled => `<span class="badge ${enabled ? "badge-running" : "badge-stopped"}">${enabled ? "Etkin" : "Devre Dışı"}</span>`;
    const toggleButton = (action, item) => `<button type="button" class="btn btn-secondary btn-sm" data-k8s-action="${action}" data-id="${esc(item.id)}">${item.enabled ? "Devre Dışı Bırak" : "Etkinleştir"}</button>`;

    const resetProjectForm = () => {
      form.reset();
      editing = null;
      form.elements.rfs_code.readOnly = false;
      form.elements.namespace.readOnly = false;
      saveButton.textContent = "RFS Kodu Ekle";
      cancelButton.hidden = true;
    };
    cancelButton.addEventListener("click", () => { resetProjectForm(); setStatus(status, ""); });

    const applicationRow = application => `<tr>
      <td class="data-value"><code>${esc(application.repository)}</code></td>
      <td>${esc(application.mlflow_model_name)}</td>
      <td>${esc(application.last_version || "—")} <span class="text-muted">· ${application.release_count} yayın</span></td>
      <td>${stateBadge(application.enabled)}</td>
      <td style="white-space:nowrap;">
        <button type="button" class="btn btn-secondary btn-sm" data-k8s-action="rebind-app" data-id="${esc(application.id)}">Modeli Değiştir</button>
        ${toggleButton("toggle-app", application)}
        ${application.locked ? "" : `<button type="button" class="btn btn-danger btn-sm" data-k8s-action="delete-app" data-id="${esc(application.id)}">Sil</button>`}
      </td>
    </tr>`;

    const projectBlock = project => `<article class="card" style="margin-top:1rem;padding:1rem;">
      <div class="card-header">
        <div>
          <h3 class="card-title" style="font-size:1rem;"><code>${esc(project.rfs_code)}</code> · ${esc(project.name)}</h3>
          <p class="text-muted" style="margin:.25rem 0 0;">namespace <code>${esc(project.namespace)}</code></p>
        </div>
        <div style="display:flex;gap:.4rem;align-items:center;flex-wrap:wrap;">
          ${stateBadge(project.enabled)}
          <button type="button" class="btn btn-secondary btn-sm" data-k8s-action="edit-project" data-id="${esc(project.id)}">Düzenle</button>
          ${toggleButton("toggle-project", project)}
          ${project.applications.length ? "" : `<button type="button" class="btn btn-danger btn-sm" data-k8s-action="delete-project" data-id="${esc(project.id)}">Sil</button>`}
        </div>
      </div>
      ${project.applications.length ? `<div class="data-table-shell">
        <table class="table"><thead><tr><th>Uygulama</th><th>MLflow modeli</th><th>Son versiyon</th><th>Durum</th><th>İşlem</th></tr></thead>
        <tbody>${project.applications.map(applicationRow).join("")}</tbody></table>
      </div>` : '<p class="text-muted" style="margin:0;">Henüz uygulama yok; ilk yayında oluşur.</p>'}
    </article>`;

    const load = async () => {
      const data = await request("/api/admin/k8s-projects");
      projects = data.projects || [];
      list.innerHTML = projects.map(projectBlock).join("") || '<p class="text-muted">Henüz RFS kodu tanımlanmadı.</p>';
    };

    form.addEventListener("submit", async event => {
      event.preventDefault();
      const fields = {
        rfs_code: form.elements.rfs_code.value.trim(),
        name: form.elements.name.value.trim(),
        namespace: form.elements.namespace.value.trim(),
      };
      saveButton.disabled = true;
      setStatus(status, "Kaydediliyor...");
      try {
        await request(
          editing ? `/api/admin/k8s-projects/${encodeURIComponent(editing.id)}` : "/api/admin/k8s-projects",
          {method: editing ? "PUT" : "POST", body: JSON.stringify(editing ? projectBody(editing, fields) : fields)},
        );
        resetProjectForm();
        await load();
        setStatus(status, "Kaydedildi.", "success");
      } catch (error) {
        setStatus(status, error.message, "error");
      } finally {
        saveButton.disabled = false;
      }
    });

    list.addEventListener("click", async event => {
      const button = event.target.closest("[data-k8s-action]");
      if (!button) return;
      const {k8sAction: action, id} = button.dataset;
      const project = projects.find(item => item.id === id);
      const application = projects.flatMap(item => item.applications).find(item => item.id === id);
      const projectUrl = `/api/admin/k8s-projects/${encodeURIComponent(id)}`;
      const applicationUrl = `/api/admin/k8s-applications/${encodeURIComponent(id)}`;
      let call;
      if (action === "edit-project" && project) {
        editing = project;
        form.elements.rfs_code.value = project.rfs_code;
        form.elements.name.value = project.name;
        form.elements.namespace.value = project.namespace;
        form.elements.rfs_code.readOnly = project.locked;
        form.elements.namespace.readOnly = project.locked;
        saveButton.textContent = "Güncelle";
        cancelButton.hidden = false;
        form.scrollIntoView({behavior: "smooth", block: "start"});
        return;
      }
      if (action === "toggle-project" && project) {
        call = [projectUrl, "PUT", projectBody(project, {enabled: !project.enabled})];
      } else if (action === "delete-project" && project) {
        if (!confirm(`${project.rfs_code} silinsin mi?`)) return;
        call = [projectUrl, "DELETE"];
      } else if (action === "toggle-app" && application) {
        call = [applicationUrl, "PUT", applicationBody(application, {enabled: !application.enabled})];
      } else if (action === "rebind-app" && application) {
        const model = (window.prompt(`${application.repository} hangi MLflow modeline bağlansın?`, application.mlflow_model_name) || "").trim();
        if (!model || model === application.mlflow_model_name) return;
        call = [applicationUrl, "PUT", applicationBody(application, {mlflow_model_name: model})];
      } else if (action === "delete-app" && application) {
        if (!confirm(`${application.repository} silinsin mi?`)) return;
        call = [applicationUrl, "DELETE"];
      } else {
        return;
      }
      const [url, method, body] = call;
      button.disabled = true;
      try {
        await request(url, body ? {method, body: JSON.stringify(body)} : {method});
        await load();
      } catch (error) {
        button.disabled = false;
        window.alert(error.message);
      }
    });

    load().catch(error => { list.innerHTML = `<p class="text-destructive">${esc(error.message)}</p>`; });
  }

  function initBamboo() {
    const form = document.getElementById("bamboo-settings-form");
    if (!form) return;
    const status = document.getElementById("bamboo-form-status");
    const badge = document.getElementById("bamboo-status-badge");
    const saveButton = document.getElementById("btn-save-bamboo");
    form.addEventListener("submit", async event => {
      event.preventDefault();
      saveButton.disabled = true;
      setStatus(status, "Kaydediliyor...");
      const token = String(form.elements.token.value || "").trim();
      try {
        const saved = await request("/api/admin/bamboo-settings", {
          method: "PUT",
          body: JSON.stringify({
            enabled: form.elements.enabled.checked,
            base_url: String(form.elements.base_url.value || "").trim(),
            plan_key: String(form.elements.plan_key.value || "").trim(),
            token: token || null,
            tls_verify: form.elements.tls_verify.checked,
          }),
        });
        form.elements.token.value = "";
        form.elements.token.placeholder = saved.has_token ? "Kayıtlı tokenı korumak için boş bırakın" : "Bamboo erişim tokenını girin";
        form.elements.plan_key.value = saved.plan_key;
        form.elements.base_url.value = saved.base_url;
        badge.className = `badge ${saved.enabled ? "badge-running" : "badge-stopped"}`;
        badge.textContent = saved.enabled ? "Etkin" : "Devre Dışı";
        setStatus(status, "Bamboo ayarları şifreli olarak kaydedildi.", "success");
      } catch (error) {
        setStatus(status, error.message, "error");
      } finally {
        saveButton.disabled = false;
      }
    });
  }

  initDeployRegistry();
  initBamboo();
  initProjects();
})();
