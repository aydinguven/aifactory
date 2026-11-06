(() => {
  "use strict";

  const esc = value => String(value ?? "").replace(/[&<>'"]/g, character => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[character]);
  const fetchJson = async (url, options = {}) => {
    const response = await fetch(url, {cache: "no-store", ...options});
    const data = response.status === 204 ? {} : await response.json().catch(() => ({}));
    if (!response.ok) {
      let detail = data.detail || `İstek başarısız (${response.status})`;
      if (Array.isArray(detail)) detail = detail.map(item => item.msg).join("; ");
      throw new Error(detail);
    }
    return data;
  };
  const terminalStatuses = new Set(["running", "failed", "stopped"]);
  const releaseTerminalStatuses = new Set(["published", "failed"]);
  const RELEASE_VERSION = /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/;
  // Kubernetes publishes the image to the deploy registry (Nexus); availability
  // comes from /api/mlflow/kubernetes/options for the selected model.
  const DEPLOY_TARGETS = {
    "ai-factory": {label: "AI Factory", title: "Modeli Dağıt · AI Factory", submit: "Model Servisini Oluştur", available: true},
    kubernetes: {label: "Kubernetes", title: "Modeli Dağıt · Kubernetes", submit: "Nexus'a Yayınla", available: false},
  };
  const statusBadge = value => value === "running" || value === "published" ? "badge-running" : value === "failed" ? "badge-error" : "badge-stopped";
  // Turkish labels: the raw values upper-cased under lang="tr" read "FAİLED".
  const STATUS_LABELS = {
    queued: "Kuyrukta", waiting_for_image: "İmaj bekleniyor", scheduling: "Planlanıyor", starting: "Başlatılıyor",
    health_checking: "Sağlık kontrolü", running: "Çalışıyor", stopped: "Durduruldu", failed: "Başarısız",
    deleting: "Siliniyor", building: "Oluşturuluyor", publishing: "Yayınlanıyor", published: "Yayınlandı",
  };
  const statusLabel = value => STATUS_LABELS[value] || value;

  // Jobs run on the controller and store every step as an event, so a log can
  // be reopened at any time. Both the deploy modal and the log viewer use these.
  const JOB_API = {
    deployment: id => `/api/mlflow/deployments/${encodeURIComponent(id)}`,
    release: id => `/api/mlflow/image-releases/${encodeURIComponent(id)}`,
  };
  const logHash = (kind, id) => `#log=${kind}:${encodeURIComponent(id)}`;
  const bambooActive = new Set(["pending", "triggering"]);
  const jobFinished = (kind, job) => kind === "release"
    ? releaseTerminalStatuses.has(job.status) && !bambooActive.has(job.bamboo_status)
    : terminalStatuses.has(job.status);
  const formatEvents = (eventData, buildLog) => {
    const lines = (eventData.events || []).map(item => {
      const when = new Date(item.created_at);
      return `[${when.toLocaleDateString("tr-TR")} ${when.toLocaleTimeString("tr-TR")}] ${item.message}`;
    });
    // A failed container build is only explainable from the build log, so
    // append it verbatim under the progress events instead of hiding it.
    if (buildLog) lines.push("", "----- image build günlüğü -----", buildLog);
    return lines.join("\n");
  };

  function initDeployModal() {
    const modal = document.getElementById("mlflow-deploy-modal");
    if (!modal) return;
    const form = document.getElementById("mlflow-deploy-form");
    const progress = document.getElementById("mlflow-deploy-progress");
    const close = document.getElementById("mlflow-deploy-close");
    const cancel = document.getElementById("mlflow-deploy-cancel");
    const submit = document.getElementById("mlflow-deploy-submit");
    const error = document.getElementById("mlflow-deploy-error");
    const flavor = document.getElementById("mlflow-deploy-flavor");
    const name = document.getElementById("mlflow-deploy-name");
    const title = document.getElementById("mlflow-deploy-title");
    const k8s = {
      badge: document.getElementById("mlflow-deploy-k8s-badge"),
      note: document.getElementById("mlflow-deploy-k8s-note"),
      project: document.getElementById("mlflow-deploy-k8s-project"),
      app: document.getElementById("mlflow-deploy-k8s-app"),
      apps: document.getElementById("mlflow-deploy-k8s-apps"),
      version: document.getElementById("mlflow-deploy-k8s-version"),
      namespace: document.getElementById("mlflow-deploy-k8s-namespace"),
      last: document.getElementById("mlflow-deploy-k8s-last"),
      image: document.getElementById("mlflow-deploy-k8s-image"),
      result: document.getElementById("mlflow-deploy-k8s-result"),
    };
    const targetOptions = Array.from(modal.querySelectorAll("[data-deploy-target-option]"));
    const targetPanels = Array.from(modal.querySelectorAll("[data-deploy-target-panel]"));
    let target = "ai-factory";
    let k8sOptions = null;

    const selectTarget = value => {
      target = DEPLOY_TARGETS[value] ? value : "ai-factory";
      const info = DEPLOY_TARGETS[target];
      targetOptions.forEach(option => {
        const selected = option.dataset.deployTargetOption === target;
        option.setAttribute("aria-checked", String(selected));
        option.classList.toggle("is-selected", selected);
      });
      targetPanels.forEach(panel => { panel.hidden = panel.dataset.deployTargetPanel !== target; });
      // A hidden required field would block the form; only the active target's fields are required.
      flavor.required = target === "ai-factory";
      name.required = target === "ai-factory";
      k8s.project.required = target === "kubernetes";
      k8s.app.required = target === "kubernetes";
      k8s.version.required = target === "kubernetes";
      title.textContent = info.title;
      submit.textContent = info.submit;
      submit.disabled = !info.available;
      error.textContent = "";
    };
    targetOptions.forEach(option => option.addEventListener("click", () => selectTarget(option.dataset.deployTargetOption)));
    const events = document.getElementById("mlflow-deploy-events");
    const result = document.getElementById("mlflow-deploy-result");
    const status = document.getElementById("mlflow-deploy-status");
    let flavorsLoaded = false;
    let activeDeployment = null;
    let activeRelease = null;
    let accessToken = "";

    const closeModal = () => {
      modal.classList.remove("open");
      activeDeployment = null;
      activeRelease = null;
    };
    close.addEventListener("click", closeModal);
    cancel.addEventListener("click", closeModal);
    modal.addEventListener("click", event => { if (event.target === modal && !submit.disabled) closeModal(); });

    async function loadFlavors() {
      if (flavorsLoaded) return;
      const items = await fetchJson("/api/workspaces/flavors");
      const available = items.filter(item => item.enabled !== false && item.available !== false);
      flavor.innerHTML = available.map(item => `<option value="${esc(item.id)}">${esc(item.display_name || item.name)} · ${esc(item.cpus)} CPU · ${esc(item.memory_display)}</option>`).join("")
        || '<option value="">Kullanılabilir kaynak profili yok</option>';
      flavorsLoaded = true;
    }

    // The user picks an admin-defined RFS code and types the app name; an unknown
    // name is created by the release and bound to this model.
    const selectedProject = () => (k8sOptions?.projects || []).find(item => item.id === k8s.project.value) || null;
    const projectApplications = () => (k8sOptions?.applications || []).filter(item => item.project_id === k8s.project.value);
    const appName = () => k8s.app.value.trim().toLowerCase();
    const existingApplication = () => projectApplications().find(item => item.app_name === appName()) || null;
    const renderK8sSummary = () => {
      const project = selectedProject();
      const application = existingApplication();
      const version = k8s.version.value.trim();
      k8s.namespace.textContent = project ? project.namespace : "—";
      k8s.last.textContent = !project || !appName() ? "—" : application ? (application.last_version || "Henüz yayın yok") : "Yeni uygulama · ilk yayın";
      const registry = k8sOptions?.registry_url ? `${k8sOptions.registry_url}/` : "";
      k8s.image.textContent = project ? `${registry}${project.rfs_code}/${appName() || "uygulama-adı"}:${RELEASE_VERSION.test(version) ? version : "X.Y.Z"}` : "—";
    };
    const suggestVersion = () => {
      k8s.version.value = existingApplication()?.suggested_version || "1.0.0";
    };
    const selectProject = () => {
      const project = selectedProject();
      const applications = projectApplications();
      k8s.apps.innerHTML = applications.map(item => `<option value="${esc(item.app_name)}"></option>`).join("");
      k8s.app.placeholder = project ? `${project.rfs_code}ml-uygulama` : "";
      k8s.app.value = applications.length ? applications[0].app_name : "";
      suggestVersion();
      renderK8sSummary();
    };
    k8s.project.addEventListener("change", selectProject);
    k8s.app.addEventListener("input", renderK8sSummary);
    k8s.app.addEventListener("change", () => { suggestVersion(); renderK8sSummary(); });
    k8s.version.addEventListener("input", renderK8sSummary);

    async function loadK8sOptions(model) {
      k8sOptions = null;
      DEPLOY_TARGETS.kubernetes.available = false;
      if (target === "kubernetes") submit.disabled = true;
      k8s.badge.textContent = "Kontrol ediliyor";
      k8s.badge.className = "badge badge-neutral";
      k8s.project.innerHTML = '<option value="">Yükleniyor...</option>';
      k8s.apps.innerHTML = "";
      k8s.app.value = "";
      k8s.version.value = "";
      k8s.note.hidden = true;
      renderK8sSummary();
      try {
        k8sOptions = await fetchJson(`/api/mlflow/kubernetes/options?model_name=${encodeURIComponent(model)}`);
      } catch (value) {
        k8sOptions = {enabled: false, projects: [], applications: [], message: value.message};
      }
      const projects = k8sOptions.projects || [];
      k8s.project.innerHTML = projects.map(item => `<option value="${esc(item.id)}">${esc(item.rfs_code)} · ${esc(item.name)}</option>`).join("")
        || '<option value="">Tanımlı RFS kodu yok</option>';
      // Default to the project this model was published under before.
      const previous = (k8sOptions.applications || [])[0];
      if (previous) k8s.project.value = previous.project_id;
      selectProject();
      DEPLOY_TARGETS.kubernetes.available = Boolean(k8sOptions.enabled);
      k8s.badge.textContent = k8sOptions.enabled ? "Hazır" : "Kullanılamıyor";
      k8s.badge.className = `badge ${k8sOptions.enabled ? "badge-running" : "badge-neutral"}`;
      k8s.note.textContent = k8sOptions.message || "";
      k8s.note.hidden = !k8sOptions.message;
      renderK8sSummary();
      if (target === "kubernetes") selectTarget("kubernetes");
    }

    const renderEvents = (eventData, buildLog) => {
      events.textContent = formatEvents(eventData, buildLog);
      events.scrollTop = events.scrollHeight;
    };
    const logLink = document.getElementById("mlflow-deploy-log-link");

    async function refreshDeployment() {
      if (!activeDeployment) return;
      const [deployment, eventData] = await Promise.all([
        fetchJson(`/api/mlflow/deployments/${encodeURIComponent(activeDeployment)}`),
        fetchJson(`/api/mlflow/deployments/${encodeURIComponent(activeDeployment)}/events`),
      ]);
      status.textContent = statusLabel(deployment.status);
      status.className = `badge ${statusBadge(deployment.status)}`;
      renderEvents(eventData, deployment.build_error_message);
      if (deployment.status === "running") {
        result.hidden = false;
        document.getElementById("mlflow-deploy-endpoint").textContent = new URL(deployment.endpoint_url, location.origin).href;
        document.getElementById("mlflow-deploy-token").value = accessToken;
        document.getElementById("mlflow-deploy-token-wrap").hidden = !accessToken;
      }
      if (!terminalStatuses.has(deployment.status)) {
        window.setTimeout(() => refreshDeployment().catch(value => { error.textContent = value.message; }), 2000);
      } else if (deployment.status === "failed") {
        error.textContent = deployment.error_message || "Deployment başarısız oldu.";
      }
    }

    async function refreshRelease() {
      if (!activeRelease) return;
      const releaseId = activeRelease;
      const [release, eventData] = await Promise.all([
        fetchJson(`/api/mlflow/image-releases/${encodeURIComponent(releaseId)}`),
        fetchJson(`/api/mlflow/image-releases/${encodeURIComponent(releaseId)}/events`),
      ]);
      if (activeRelease !== releaseId) return;
      status.textContent = statusLabel(release.status);
      status.className = `badge ${statusBadge(release.status)}`;
      renderEvents(eventData, release.status === "failed" ? release.build_error_message : "");
      if (release.status === "published") {
        k8s.result.hidden = false;
        document.getElementById("mlflow-deploy-k8s-result-image").textContent = release.image_ref;
        document.getElementById("mlflow-deploy-k8s-result-digest").textContent = release.digest;
      }
      if (!releaseTerminalStatuses.has(release.status)) {
        window.setTimeout(() => refreshRelease().catch(value => { error.textContent = value.message; }), 2000);
      } else if (release.status === "failed") {
        error.textContent = release.error_message || "Yayın başarısız oldu.";
      }
    }

    document.addEventListener("click", event => {
      const button = event.target.closest("[data-deploy-model]");
      if (!button) return;
      const model = button.dataset.modelName || "";
      const version = button.dataset.modelVersion || "";
      const run = button.dataset.runId || "";
      form.hidden = false;
      progress.hidden = true;
      result.hidden = true;
      k8s.result.hidden = true;
      error.textContent = "";
      activeDeployment = null;
      activeRelease = null;
      accessToken = "";
      selectTarget(button.dataset.deployTarget || "ai-factory");
      document.getElementById("mlflow-deploy-model-name").value = model;
      document.getElementById("mlflow-deploy-model-version").value = version;
      document.getElementById("mlflow-deploy-selection").textContent = `${model} · v${version}`;
      document.getElementById("mlflow-deploy-run").textContent = run ? `Run: ${run}` : "";
      name.value = `${model}-v${version}`.replace(/[^a-zA-Z0-9_-]+/g, "-").slice(0, 60);
      modal.classList.add("open");
      loadFlavors().catch(value => { error.textContent = value.message; });
      loadK8sOptions(model);
    });

    const startProgress = (kind, id) => {
      form.hidden = true;
      progress.hidden = false;
      events.textContent = "";
      if (logLink) logLink.href = `/deployments${logHash(kind, id)}`;
    };

    async function submitDeployment() {
      const deployment = await fetchJson("/api/mlflow/deployments", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          name: name.value.trim(),
          model_name: document.getElementById("mlflow-deploy-model-name").value,
          model_version: document.getElementById("mlflow-deploy-model-version").value,
          flavor_id: flavor.value,
          auto_stop_minutes: Number(document.getElementById("mlflow-deploy-auto-stop").value || 0),
          gunicorn_workers: Number(document.getElementById("mlflow-deploy-workers").value || 1),
        }),
      });
      activeDeployment = deployment.id;
      accessToken = deployment.access_token;
      startProgress("deployment", deployment.id);
      result.hidden = false;
      document.getElementById("mlflow-deploy-endpoint").textContent = new URL(deployment.endpoint_url, location.origin).href;
      document.getElementById("mlflow-deploy-token").value = accessToken;
      document.getElementById("mlflow-deploy-token-wrap").hidden = false;
      await refreshDeployment();
    }

    async function submitRelease() {
      const project = selectedProject();
      const app = appName();
      const version = k8s.version.value.trim();
      if (!project) throw new Error("Bir RFS kodu seçin.");
      if (!app.startsWith(project.rfs_code)) throw new Error(`Uygulama adı RFS koduyla başlamalıdır (örn. ${project.rfs_code}ml-uygulama).`);
      if (!RELEASE_VERSION.test(version)) throw new Error("Versiyon X.Y.Z biçiminde olmalıdır (örn. 1.0.0).");
      const release = await fetchJson("/api/mlflow/image-releases", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          project_id: project.id,
          app_name: app,
          model_name: document.getElementById("mlflow-deploy-model-name").value,
          model_version: document.getElementById("mlflow-deploy-model-version").value,
          version,
        }),
      });
      activeRelease = release.id;
      startProgress("release", release.id);
      await refreshRelease();
    }

    form.addEventListener("submit", async event => {
      event.preventDefault();
      if (!DEPLOY_TARGETS[target].available) {
        error.textContent = k8sOptions?.message || "Bu hedef şu anda kullanılamıyor.";
        return;
      }
      error.textContent = "";
      submit.disabled = true;
      submit.textContent = "Kuyruğa alınıyor...";
      try {
        if (target === "kubernetes") await submitRelease();
        else await submitDeployment();
      } catch (value) {
        error.textContent = value.message;
        submit.disabled = false;
        submit.textContent = DEPLOY_TARGETS[target].submit;
      }
    });
  }

  function initDeploymentList() {
    const root = document.getElementById("mlflow-deployment-list");
    if (!root) return;
    const status = document.getElementById("mlflow-deployment-list-status");
    const body = document.getElementById("mlflow-deployment-list-body");
    const actions = deployment => {
      let lifecycle = "";
      if (deployment.status === "running") lifecycle = `<button class="btn btn-secondary btn-sm" data-deployment-action="stop" data-deployment-id="${esc(deployment.id)}">Durdur</button>`;
      if (deployment.status === "stopped") lifecycle = `<button class="btn btn-primary btn-sm" data-deployment-action="start" data-deployment-id="${esc(deployment.id)}">Başlat</button>`;
      if (deployment.status === "failed") lifecycle = `<button class="btn btn-primary btn-sm" data-deployment-action="retry" data-deployment-id="${esc(deployment.id)}">Yeniden Dene</button>`;
      return `${lifecycle} <button class="btn btn-secondary btn-sm" data-deployment-action="token" data-deployment-id="${esc(deployment.id)}">Token Yenile</button>`;
    };
    async function load() {
      const data = await fetchJson("/api/mlflow/deployments");
      body.innerHTML = (data.deployments || []).map(item => `<tr>
        <td class="mlflow-job-name"><strong>${esc(item.name)}</strong><br><span class="text-muted">${esc(item.model_name)} v${esc(item.model_version)}</span></td>
        <td class="mlflow-job-nowrap"><span class="badge badge-neutral">${esc(DEPLOY_TARGETS[item.target || "ai-factory"]?.label || item.target)}</span></td>
        <td class="mlflow-job-status"><span class="badge ${statusBadge(item.status)}">${esc(statusLabel(item.status))}</span><br><span class="text-muted mlflow-job-message" title="${esc(item.status_message || "")}">${esc(item.status_message || "")}</span></td>
        <td class="mlflow-job-nowrap">${esc(item.flavor_id)}</td>
        <td class="data-value mlflow-job-ref"><code>${esc(item.endpoint_url)}</code></td>
        <td class="mlflow-job-actions">${logButton("deployment", item.id)} ${actions(item)} ${terminalStatuses.has(item.status) ? `<button class="btn btn-danger btn-sm" data-deployment-action="delete" data-deployment-id="${esc(item.id)}">Sil</button>` : ""}</td>
      </tr>`).join("") || '<tr><td colspan="6" class="text-muted">Henüz model deployment oluşturulmadı.</td></tr>';
      status.textContent = "";
    }
    root.addEventListener("click", async event => {
      const button = event.target.closest("[data-deployment-action]");
      if (!button) return;
      const action = button.dataset.deploymentAction;
      if (action === "delete" && !confirm("Model deployment ve servis workspace silinsin mi?")) return;
      button.disabled = true;
      try {
        const actionResult = await fetchJson(`/api/mlflow/deployments/${encodeURIComponent(button.dataset.deploymentId)}${action === "delete" ? "" : `/${action}`}`, {method: action === "delete" ? "DELETE" : "POST"});
        if (action === "token" && actionResult.access_token) {
          window.prompt("Yeni erişim tokenı — şimdi güvenli bir yere kopyalayın:", actionResult.access_token);
        }
        await load();
      } catch (value) {
        status.textContent = value.message;
        status.className = "text-destructive";
      } finally {
        button.disabled = false;
      }
    });
    load().catch(value => { status.textContent = value.message; status.className = "text-destructive"; });
    window.setInterval(() => load().catch(() => {}), 5000);
  }

  function initReleaseList() {
    const root = document.getElementById("mlflow-release-list");
    if (!root) return;
    const status = document.getElementById("mlflow-release-list-status");
    const body = document.getElementById("mlflow-release-list-body");
    const imageCell = item => item.status === "published"
      ? `<code>${esc(item.image_ref)}</code><br><span class="text-muted" title="${esc(item.digest)}">${esc(item.digest.slice(0, 19))}…</span>`
      : `<code>${esc(item.image_ref || `${item.rfs_code}/${item.app_name}:${item.version}`)}</code>`;
    const bambooLabels = {pending: "bekliyor", triggering: "tetikleniyor", queued: "kuyrukta", failed: "başarısız"};
    const bambooLine = item => {
      if (!item.bamboo_status) return "";
      const label = `Bamboo: ${bambooLabels[item.bamboo_status] || item.bamboo_status}`;
      const key = item.bamboo_result_key
        ? (item.bamboo_result_url
          ? ` <a href="${esc(item.bamboo_result_url)}" target="_blank" rel="noopener noreferrer">${esc(item.bamboo_result_key)}</a>`
          : ` ${esc(item.bamboo_result_key)}`)
        : "";
      const kind = item.bamboo_status === "failed" ? "text-destructive" : "text-muted";
      return `<br><span class="${kind}" title="${esc(item.bamboo_error || "")}">${esc(label)}${key}</span>`;
    };
    async function load() {
      const data = await fetchJson("/api/mlflow/image-releases");
      body.innerHTML = (data.releases || []).map(item => `<tr>
        <td class="mlflow-job-name"><strong>${esc(item.rfs_code)}/${esc(item.app_name)}</strong><br><span class="text-muted">${esc(item.model_name)} v${esc(item.model_version)}</span><br><span class="text-muted">Namespace: ${esc(item.namespace)}</span></td>
        <td class="mlflow-job-nowrap"><strong>${esc(item.version)}</strong></td>
        <td class="mlflow-job-status"><span class="badge ${statusBadge(item.status)}">${esc(statusLabel(item.status))}</span><br><span class="text-muted mlflow-job-message" title="${esc(item.error_message || item.status_message || "")}">${esc(item.status_message || "")}</span>${bambooLine(item)}</td>
        <td class="data-value mlflow-job-ref">${imageCell(item)}</td>
        <td class="mlflow-job-nowrap">${esc(item.username)}<br><span class="text-muted">${esc(new Date(item.created_at).toLocaleString("tr-TR"))}</span></td>
        <td class="mlflow-job-actions">${logButton("release", item.id)}${item.status === "failed" ? ` <button class="btn btn-primary btn-sm" data-release-retry="${esc(item.id)}">Yeniden Dene</button>` : ""}${item.bamboo_status === "failed" ? ` <button class="btn btn-secondary btn-sm" data-bamboo-retry="${esc(item.id)}">Bamboo'yu Yeniden Tetikle</button>` : ""}</td>
      </tr>`).join("") || '<tr><td colspan="6" class="text-muted">Henüz Kubernetes yayını yapılmadı.</td></tr>';
      status.textContent = "";
      status.className = "";
    }
    root.addEventListener("click", async event => {
      const button = event.target.closest("[data-release-retry], [data-bamboo-retry]");
      if (!button) return;
      const bamboo = button.hasAttribute("data-bamboo-retry");
      if (bamboo && !confirm("Bamboo planı yeniden tetiklensin mi? Önceki tetikleme Bamboo'ya ulaştıysa Jira görevleri tekrar açılabilir.")) return;
      button.disabled = true;
      try {
        const releaseId = bamboo ? button.dataset.bambooRetry : button.dataset.releaseRetry;
        await fetchJson(`/api/mlflow/image-releases/${encodeURIComponent(releaseId)}/${bamboo ? "bamboo-retry" : "retry"}`, {method: "POST"});
        await load();
      } catch (value) {
        status.textContent = value.message;
        status.className = "text-destructive";
        button.disabled = false;
      }
    });
    load().catch(value => { status.textContent = value.message; status.className = "text-destructive"; });
    window.setInterval(() => load().catch(() => {}), 5000);
  }

  // Log viewer on /deployments: opens from a row's "Günlük" button or from
  // #log=<kind>:<id> (the deploy modal links there), and follows the job live
  // until it finishes.
  function initLogViewer() {
    const modal = document.getElementById("mlflow-log-modal");
    if (!modal) return;
    const el = id => document.getElementById(id);
    const title = el("mlflow-log-title");
    const kicker = el("mlflow-log-kicker");
    const badge = el("mlflow-log-status");
    const summary = el("mlflow-log-summary");
    const events = el("mlflow-log-events");
    const footer = el("mlflow-log-footer");
    let current = null;
    let timer = null;

    const stop = () => {
      window.clearTimeout(timer);
      timer = null;
    };
    const close = () => {
      stop();
      current = null;
      modal.classList.remove("open");
      if (location.hash.startsWith("#log=")) history.replaceState(null, "", location.pathname + location.search);
    };

    async function refresh(job) {
      const base = JOB_API[job.kind](job.id);
      const [detail, eventData] = await Promise.all([fetchJson(base), fetchJson(`${base}/events`)]);
      if (current !== job) return;
      const finished = jobFinished(job.kind, detail);
      title.textContent = job.kind === "release"
        ? `${detail.rfs_code}/${detail.app_name}:${detail.version}`
        : detail.name;
      kicker.textContent = job.kind === "release"
        ? `Kubernetes yayını · ${detail.model_name} v${detail.model_version}`
        : `Model deployment · ${detail.model_name} v${detail.model_version}`;
      badge.textContent = statusLabel(detail.status);
      badge.className = `badge ${statusBadge(detail.status)}`;
      summary.textContent = detail.status === "failed" ? (detail.error_message || detail.status_message || "") : (detail.status_message || "");
      summary.className = `mlflow-log-summary ${detail.status === "failed" ? "text-destructive" : "text-muted"}`;
      // Keep the reader's scroll position unless they were following the end.
      const atEnd = events.scrollHeight - events.scrollTop - events.clientHeight < 24;
      events.textContent = formatEvents(eventData, detail.status === "failed" ? detail.build_error_message : "")
        || "Henüz kayıtlı adım yok.";
      if (atEnd || !job.rendered) events.scrollTop = events.scrollHeight;
      job.rendered = true;
      footer.textContent = finished
        ? `Tamamlandı · ${(eventData.events || []).length} adım kayıtlı.`
        : "İşlem sürüyor · günlük her 2 saniyede güncellenir. Pencereyi kapatmak işlemi durdurmaz.";
      if (!finished) timer = window.setTimeout(() => refresh(job).catch(fail), 2000);
    }
    const fail = value => {
      if (!current) return;
      summary.textContent = value.message;
      summary.className = "mlflow-log-summary text-destructive";
    };

    const open = (kind, id) => {
      if (!JOB_API[kind] || !id) return;
      stop();
      current = {kind, id, rendered: false};
      title.textContent = "Yükleniyor...";
      kicker.textContent = "Günlük";
      badge.textContent = "—";
      badge.className = "badge badge-neutral";
      summary.textContent = "";
      events.textContent = "";
      footer.textContent = "";
      modal.classList.add("open");
      history.replaceState(null, "", location.pathname + location.search + logHash(kind, id));
      refresh(current).catch(fail);
    };

    el("mlflow-log-close").addEventListener("click", close);
    modal.addEventListener("click", event => { if (event.target === modal) close(); });
    document.addEventListener("keydown", event => { if (event.key === "Escape" && current) close(); });
    document.addEventListener("click", event => {
      const button = event.target.closest("[data-log-kind]");
      if (button) open(button.dataset.logKind, button.dataset.logId);
    });
    const fromHash = () => {
      const match = /^#log=(deployment|release):(.+)$/.exec(location.hash);
      if (match) open(match[1], decodeURIComponent(match[2]));
    };
    window.addEventListener("hashchange", fromHash);
    fromHash();
  }
  const logButton = (kind, id) => `<button class="btn btn-secondary btn-sm" type="button" data-log-kind="${kind}" data-log-id="${esc(id)}">Günlük</button>`;

  initDeployModal();
  initDeploymentList();
  initReleaseList();
  initLogViewer();
})();
