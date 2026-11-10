// ==============================================================================
// AI Factory Client Application Logic
// ==============================================================================

document.addEventListener("DOMContentLoaded", () => {
  initInlineEditControls();
  initFilePickers();
  initDialogNavigation();
  initWorkspaceFilters();
  initWorkspaceCreationModal();
  initActionButtons();
  initLogPolling();
  initQuotaForms();
  initDirectorySettings();
  initDirectorySync();
  initJupyterAiSettings();
  initKiloCodeSettings();
  initModelContainerRegistrySettings();
  initAdminMlflowSettings();
  initAdminGenAiSettings();
  initNodeManagement();
  initPlacementAdmin();
  initMlflowSettings();
  initDownloadSettings();
  initSessionSettings();
  initOnboardingSettings();
  initHttpsSettings();
  initDownloadUpdater();
  initLiveMetricsPolling();
  initWorkspaceTabs();
  initPortExposer();
  initWorkspaceShares();
  initSnapshotModal();
  initAdminPlatformUpdater();
  initUserGroupFilter();
  initAdminNavigation();
  initAdminFilters();
  initWorkspaceImageManager();
  initAdminFlavorSettings();
});

function setViewStatus(element, message, state = "") {
  if (element.dataset.state === state && element.textContent === message) return;
  element.dataset.state = state;
  element.textContent = message;
}

function initDialogNavigation() {
  const backdrops = [...document.querySelectorAll(".modal-backdrop")];
  if (!backdrops.length) return;
  let active = null;

  const canFocus = element => {
    if (!(element instanceof HTMLElement) || element === document.body || !element.getClientRects().length) return false;
    const closedDetails = element.closest("details:not([open])");
    return !closedDetails || closedDetails.querySelector(":scope > summary")?.contains(element);
  };

  const sync = () => {
    const backdrop = backdrops.findLast(element => element.classList.contains("open"));
    if (backdrop === active?.backdrop) return;
    const opener = active?.opener || document.activeElement;
    active?.background.forEach(([element, inert]) => { element.inert = inert; });
    active = null;
    if (!backdrop) {
      const target = canFocus(opener) ? opener : opener?.closest("details")?.querySelector("summary");
      (target || document.getElementById("main-content"))?.focus();
      return;
    }

    const dialog = backdrop.querySelector('[role="dialog"]');
    const close = dialog.querySelector(".modal-close");
    const background = [];
    for (let branch = backdrop; branch !== document.body; branch = branch.parentElement) {
      for (const sibling of branch.parentElement.children) {
        if (sibling === branch) continue;
        background.push([sibling, sibling.inert]);
        sibling.inert = true;
      }
    }
    active = {backdrop, dialog, close, opener, background};
    dialog.tabIndex = -1;
    window.requestAnimationFrame(() => {
      if (active?.backdrop === backdrop && !dialog.contains(document.activeElement)) {
        (close || dialog).focus({preventScroll: true});
      }
    });
  };

  // Existing close handlers own workflow state, including in-flight deployments.
  const observer = new MutationObserver(sync);
  backdrops.forEach(backdrop => observer.observe(backdrop, {attributes: true, attributeFilter: ["class"]}));
  sync();
  document.addEventListener("keydown", event => {
    if (!active || event.defaultPrevented) return;
    if (event.key === "Escape") {
      event.preventDefault();
      active.close?.click();
    }
    if (event.key !== "Tab") return;
    const {dialog} = active;
    const focusable = [...dialog.querySelectorAll("button, a[href], input, select, textarea, summary, [tabindex]")]
      .filter(element => element.tabIndex >= 0 && !element.matches(":disabled") && canFocus(element));
    const edge = event.shiftKey ? focusable[0] : focusable.at(-1);
    if (!edge || document.activeElement === edge || document.activeElement === dialog) {
      event.preventDefault();
      const next = event.shiftKey ? focusable.at(-1) : focusable[0];
      (next || dialog).focus();
    }
  });
}

function normalizeSearch(value) {
  return value.trim().toLocaleLowerCase("tr").normalize("NFD")
    .replace(/\p{Mark}/gu, "").replaceAll("ı", "i");
}

function initWorkspaceFilters() {
  const filters = document.querySelector("[data-workspace-filters]");
  if (!filters) return;
  const search = document.getElementById("workspace-search");
  const status = document.getElementById("workspace-status-filter");
  const clear = document.getElementById("workspace-filters-clear");
  const count = document.getElementById("workspace-result-count");
  const noResults = document.getElementById("workspace-no-results");
  const pendingStatuses = ["creating", "starting", "stopping"];
  const workspaces = [...document.querySelectorAll("#workspace-results [data-workspace-search]")].map(card => ({
    card,
    searchText: normalizeSearch(card.dataset.workspaceSearch),
    status: pendingStatuses.includes(card.dataset.workspaceStatus) ? "pending" : card.dataset.workspaceStatus,
  }));

  const applyFilters = () => {
    const query = normalizeSearch(search.value);
    const terms = query ? query.split(/\s+/) : [];
    let visible = 0;
    for (const workspace of workspaces) {
      workspace.card.hidden = !terms.every(term => workspace.searchText.includes(term))
        || Boolean(status.value && status.value !== workspace.status);
      if (!workspace.card.hidden) visible++;
    }
    clear.disabled = !search.value && !status.value;
    count.textContent = `${workspaces.length} çalışma alanından ${visible} tanesi gösteriliyor.`;
    noResults.hidden = visible !== 0;
  };
  search.addEventListener("input", applyFilters);
  status.addEventListener("change", applyFilters);
  clear.addEventListener("click", () => {
    search.value = "";
    status.value = "";
    search.focus();
    applyFilters();
  });
  // Browsers may restore filter inputs when navigating back from a workspace.
  window.addEventListener("pageshow", applyFilters);
  filters.hidden = false;
  applyFilters();
}

function initAdminNavigation() {
  const toggle = document.getElementById("admin-category-toggle");
  if (!toggle) return;
  const links = document.getElementById("admin-category-links");
  const compact = window.matchMedia("(max-width: 760px)");
  const setExpanded = expanded => {
    toggle.setAttribute("aria-expanded", String(expanded));
    links.hidden = !expanded;
  };
  const updateLayout = () => {
    const focused = document.activeElement;
    toggle.hidden = !compact.matches;
    setExpanded(!compact.matches);
    if (compact.matches && links.contains(focused)) toggle.focus();
    if (!compact.matches && focused === toggle) links.querySelector("[aria-current]")?.focus();
  };
  toggle.addEventListener("click", () => setExpanded(links.hidden));
  toggle.parentElement.addEventListener("keydown", event => {
    if (event.key !== "Escape" || toggle.hidden || links.hidden) return;
    event.preventDefault();
    setExpanded(false);
    toggle.focus();
  });
  compact.addEventListener("change", updateLayout);
  updateLayout();
}

function initInlineEditControls(scope = document) {
  scope.querySelectorAll("[data-inline-edit-input]").forEach((input) => {
    if (input.dataset.inlineEditEnhanced === "true" || input.disabled) return;
    input.dataset.inlineEditEnhanced = "true";
    input.readOnly = true;
    input.setAttribute("aria-readonly", "true");
    if (!input.id) input.id = `inline-edit-${Math.random().toString(36).slice(2)}`;
    const wrapper = document.createElement("div");
    wrapper.className = "inline-edit-control";
    input.parentNode.insertBefore(wrapper, input);
    wrapper.appendChild(input);
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "inline-edit-toggle";
    toggle.dataset.inlineEditToggle = "";
    toggle.setAttribute("aria-controls", input.id);
    const label = input.dataset.inlineEditLabel || "Alanı";
    const setEditing = (editing, focus = true) => {
      input.readOnly = !editing;
      input.setAttribute("aria-readonly", String(!editing));
      wrapper.classList.toggle("is-editing", editing);
      toggle.setAttribute("aria-pressed", String(editing));
      toggle.textContent = editing ? "Bitti" : "Düzenle";
      toggle.setAttribute("aria-label", `${label} ${editing ? "düzenlemeyi bitir" : "düzenle"}`);
      if (focus) (editing ? input : toggle).focus();
      if (editing && input.type !== "number") input.select();
    };
    toggle.addEventListener("click", () => setEditing(input.readOnly));
    input.addEventListener("keydown", (event) => {
      if ((event.key === "Enter" || event.key === "Escape") && !input.readOnly) {
        event.preventDefault();
        setEditing(false);
      }
    });
    input._setInlineEditing = setEditing;
    wrapper.appendChild(toggle);
    setEditing(false, false);
  });
}

function closeInlineEditControls(scope) {
  scope.querySelectorAll("[data-inline-edit-input]").forEach((input) => input._setInlineEditing?.(false, false));
}

function initFilePickers() {
  let pickerIndex = 0;
  document.querySelectorAll('input[type="file"][data-file-picker-label]').forEach((input) => {
    if (input.dataset.filePickerEnhanced === "true") return;
    input.dataset.filePickerEnhanced = "true";
    pickerIndex += 1;

    if (!input.id) input.id = `file-picker-input-${pickerIndex}`;
    const picker = document.createElement("div");
    picker.className = "file-picker";
    input.parentNode.insertBefore(picker, input);
    picker.appendChild(input);
    input.classList.add("file-picker-input");

    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "btn btn-secondary file-picker-trigger";
    trigger.textContent = input.dataset.filePickerLabel || "Dosya Seç";
    trigger.setAttribute("aria-controls", input.id);

    const selection = document.createElement("span");
    selection.id = `${input.id}-selection`;
    selection.className = "file-picker-selection";
    selection.setAttribute("role", "status");
    selection.setAttribute("aria-live", "polite");
    selection.setAttribute("dir", "auto");
    trigger.setAttribute("aria-describedby", selection.id);
    input.setAttribute(
      "aria-describedby",
      [input.getAttribute("aria-describedby"), selection.id].filter(Boolean).join(" "),
    );

    const sync = () => {
      const files = [...(input.files || [])];
      const filenames = files.map((file) => file.name);
      selection.textContent = files.length === 0
        ? "Dosya seçilmedi"
        : files.length === 1
          ? filenames[0]
          : `${files.length.toLocaleString("tr-TR")} dosya seçildi`;
      selection.title = filenames.join(", ");
      picker.classList.toggle("has-selection", files.length > 0);
      trigger.disabled = input.disabled;
    };

    trigger.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      input.click();
    });
    input.addEventListener("change", sync);
    input.form?.addEventListener("reset", () => window.setTimeout(sync, 0));

    picker.append(trigger, selection);
    picker.classList.add("is-enhanced");
    sync();
  });
}

function initAdminFlavorSettings() {
  const table = document.getElementById("admin-flavor-settings-table");
  if (!table) return;
  const status = document.getElementById("admin-flavor-settings-status");

  const request = async (url, options = {}) => {
    const response = await fetch(url, options);
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || `İşlem başarısız (${response.status})`);
    return result;
  };
  const show = (message, error = false) => {
    status.textContent = message;
    status.className = `quota-form-status ${error ? "quota-status-error" : "quota-status-success"}`;
  };
  const inputsFor = (row) => [...row.querySelectorAll("[data-flavor-field]")];
  const updateSaveState = (row) => {
    const inputs = inputsFor(row);
    const changed = inputs.some((input) => input.value !== input.dataset.originalValue);
    const valid = inputs.every((input) => input.checkValidity());
    const save = row.querySelector("[data-flavor-save]");
    if (save) save.disabled = !changed || !valid;
  };
  const closeEditor = (row, { restore = false, focus = true } = {}) => {
    inputsFor(row).forEach((input) => {
      if (restore) input.value = input.dataset.originalValue;
      input.disabled = true;
    });
    row.querySelectorAll("[data-flavor-view-region], [data-flavor-view-actions]").forEach((element) => { element.hidden = false; });
    row.querySelectorAll("[data-flavor-edit-region], [data-flavor-edit-actions]").forEach((element) => { element.hidden = true; });
    row.classList.remove("is-editing");
    const edit = row.querySelector("[data-flavor-edit]");
    edit?.setAttribute("aria-expanded", "false");
    if (focus) edit?.focus();
  };
  const openEditor = (row) => {
    const openRow = table.querySelector("tr.is-editing");
    if (openRow && openRow !== row) closeEditor(openRow, { restore: true, focus: false });
    inputsFor(row).forEach((input) => { input.disabled = false; });
    row.querySelectorAll("[data-flavor-view-region], [data-flavor-view-actions]").forEach((element) => { element.hidden = true; });
    row.querySelectorAll("[data-flavor-edit-region], [data-flavor-edit-actions]").forEach((element) => { element.hidden = false; });
    row.classList.add("is-editing");
    row.querySelector("[data-flavor-edit]")?.setAttribute("aria-expanded", "true");
    updateSaveState(row);
    row.querySelector('[data-flavor-field="display_name"]')?.focus();
  };

  table.addEventListener("input", (event) => {
    const row = event.target.closest("tr.is-editing");
    if (row && event.target.matches("[data-flavor-field]")) updateSaveState(row);
  });
  table.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const row = event.target.closest("tr.is-editing");
    if (row) closeEditor(row, { restore: true });
  });
  table.addEventListener("click", async (event) => {
    const edit = event.target.closest("[data-flavor-edit]");
    const cancel = event.target.closest("[data-flavor-cancel]");
    if (edit) {
      openEditor(edit.closest("tr"));
      return;
    }
    if (cancel) {
      closeEditor(cancel.closest("tr"), { restore: true });
      return;
    }

    const toggle = event.target.closest("[data-flavor-toggle]");
    const save = event.target.closest("[data-flavor-save]");
    const remove = event.target.closest("[data-flavor-delete]");
    if (!toggle && !save && !remove) return;
    const action = toggle || save || remove;
    const flavorId = action.dataset.flavorToggle || action.dataset.flavorSave || action.dataset.flavorDelete;
    if (remove && !window.confirm(`${flavorId} kaynak profili silinsin mi?`)) return;
    const row = action.closest("tr");
    action.disabled = true;
    row.setAttribute("aria-busy", "true");
    status.textContent = `${flavorId} güncelleniyor...`;
    try {
      if (remove) {
        await request(`/api/admin/flavors/${encodeURIComponent(flavorId)}`, {method:"DELETE"});
        row.remove();
        show(`${flavorId} silindi. Worker'lara Güncelle düğmesiyle kataloğu dağıtın.`);
        return;
      }
      const payload = toggle
        ? {enabled: toggle.dataset.enabled !== "true"}
        : {
            display_name: row.querySelector('[data-flavor-field="display_name"]').value.trim(),
            cpus: Number(row.querySelector('[data-flavor-field="cpus"]').value),
            memory_mb: Number(row.querySelector('[data-flavor-field="memory_mb"]').value),
          };
      const result = await request(`/api/admin/flavors/${encodeURIComponent(flavorId)}`, {method:"PATCH", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)});
      const toggleButton = row.querySelector("[data-flavor-toggle]");
      const badge = row.querySelector("[data-flavor-status]");
      toggleButton.dataset.enabled = String(result.enabled);
      toggleButton.textContent = result.enabled ? "Devre Dışı Bırak" : "Etkinleştir";
      toggleButton.setAttribute("aria-label", `${result.display_name} profilini ${result.enabled ? "devre dışı bırak" : "etkinleştir"}`);
      badge.className = `badge ${result.enabled ? "badge-running" : "badge-neutral"}`;
      badge.textContent = result.enabled ? "Etkin" : "Devre Dışı";
      if (save) {
        const values = {
          display_name: result.display_name,
          cpus: String(result.cpus),
          memory_mb: String(result.memory_mb),
        };
        Object.entries(values).forEach(([field, value]) => {
          const input = row.querySelector(`[data-flavor-field="${field}"]`);
          const output = row.querySelector(`[data-flavor-value="${field}"]`);
          input.value = value;
          input.dataset.originalValue = value;
          output.textContent = value;
        });
        row.querySelector("[data-flavor-edit]").setAttribute("aria-label", `${result.display_name} profilini düzenle`);
        row.querySelector("[data-flavor-delete]")?.setAttribute("aria-label", `${result.display_name} profilini sil`);
        closeEditor(row);
      }
      show(`${flavorId} kaydedildi. Worker'lara Güncelle düğmesiyle kataloğu dağıtın.`);
    } catch (error) {
      show(error.message, true);
    } finally {
      action.disabled = false;
      row.removeAttribute("aria-busy");
      if (save && row.classList.contains("is-editing")) updateSaveState(row);
    }
  });

  document.getElementById("admin-flavor-create-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form).entries());
    values.cpus = Number(values.cpus); values.memory_mb = Number(values.memory_mb);
    try {
      await request("/api/admin/flavors", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(values)});
      window.location.reload();
    } catch (error) { show(error.message, true); }
  });

  document.getElementById("btn-sync-workspace-catalog")?.addEventListener("click", async (event) => {
    const button = event.currentTarget; button.disabled = true; status.textContent = "Worker katalogları güncelleniyor...";
    try {
      const result = await request("/api/admin/catalog/sync", {method:"POST"});
      const failures = (result.workers || []).filter(item => !item.success);
      show(`${result.success_count}/${result.total_count} worker güncellendi${failures.length ? `; ${failures.map(item => `${item.node_name}: ${item.message}`).join(", ")}` : "."}`, Boolean(failures.length));
    } catch (error) { show(error.message, true); }
    finally { button.disabled = false; }
  });

  const templateTable = document.getElementById("admin-custom-template-table");
  const templateStatus = document.getElementById("admin-template-settings-status");
  templateTable?.addEventListener("click", async (event) => {
    const save = event.target.closest("[data-template-save]");
    const remove = event.target.closest("[data-template-delete]");
    if (!save && !remove) return;
    const row = (save || remove).closest("tr"), templateId = row.dataset.templateId;
    try {
      if (remove) {
        if (!window.confirm(`${templateId} workspace tipi ve controller image arşivleri silinsin mi?`)) return;
        await request(`/api/admin/templates/${encodeURIComponent(templateId)}`, {method:"DELETE"});
        row.remove();
        templateStatus.textContent = `${templateId} ve ilişkili controller image arşivleri silindi.`;
      } else {
        const payload = {};
        row.querySelectorAll("[data-template-field]:not(:disabled)").forEach(input => { payload[input.dataset.templateField] = input.type === "number" ? Number(input.value) : input.value.trim(); });
        await request(`/api/admin/templates/${encodeURIComponent(templateId)}`, {method:"PATCH", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)});
        closeInlineEditControls(row);
        templateStatus.textContent = `${templateId} kaydedildi.`;
      }
      templateStatus.className = "quota-form-status quota-status-success";
    } catch (error) { templateStatus.textContent = error.message; templateStatus.className = "quota-form-status quota-status-error"; }
  });
}

// Live progress of a controller or worker update (app/installer/progress.py).
const UPDATE_ACTIVE_STATES = ["preparing", "downloading", "queued", "running"];

function formatUpdateBytes(value) {
  const units = ["B", "KB", "MB", "GB"];
  let size = Number(value) || 0;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return `${size.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

function formatUpdateElapsed(startedAt) {
  const started = Date.parse(startedAt || "");
  if (Number.isNaN(started)) return "";
  const seconds = Math.max(0, Math.round((Date.now() - started) / 1000));
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")} geçti`;
}

function renderUpdateProgress(box, state, progress, { restarting = false, startedAt = "" } = {}) {
  if (!box) return;
  const active = UPDATE_ACTIVE_STATES.includes(state) || restarting;
  const finished = state === "succeeded" || state === "failed";
  if (!active && !(finished && progress)) {
    box.hidden = true;
    return;
  }
  const value = progress || {};
  let percent = Number(value.percent) || 0;
  let label = value.label || "";
  if (state === "succeeded") {
    percent = 100;
    label = "Tamamlandı";
  } else if (state === "failed") {
    label = value.phase === "rollback" || value.phase === "failed"
      ? `${value.label || "Güncelleme"} · başarısız`
      : `${label || "Güncelleme"} · başarısız`;
  } else if (restarting) {
    label = "Controller yeniden başlatılıyor";
  } else if (!label) {
    label = { preparing: "Hazırlanıyor", downloading: "Bundle indiriliyor", queued: "Root updater bekleniyor", running: "Güncelleme uygulanıyor" }[state] || "";
  }
  const parts = [];
  if (value.step_index && value.step_total && value.phase === "steps") parts.push(`Adım ${value.step_index}/${value.step_total}`);
  if (value.phase === "download" && value.bytes_total) parts.push(`${formatUpdateBytes(value.bytes_done)} / ${formatUpdateBytes(value.bytes_total)}`);
  if (value.detail) parts.push(value.detail);
  if (active) {
    const elapsed = formatUpdateElapsed(value.started_at || startedAt);
    if (elapsed) parts.push(elapsed);
  }
  const indeterminate = active && (!progress || restarting);
  box.hidden = false;
  box.classList.toggle("is-indeterminate", indeterminate);
  box.classList.toggle("is-failed", state === "failed");
  box.classList.toggle("is-rollback", value.phase === "rollback" && state !== "failed");
  box.classList.toggle("is-done", state === "succeeded");
  const fill = box.querySelector(".update-progress-track > span");
  if (fill) fill.style.width = `${indeterminate ? 100 : Math.max(2, percent)}%`;
  box.querySelector(".update-progress-track")?.setAttribute("aria-valuenow", String(Math.round(percent)));
  const labelNode = box.querySelector(".update-progress-label");
  const percentNode = box.querySelector(".update-progress-percent");
  const textNode = box.querySelector(".update-progress-text");
  if (labelNode) {
    labelNode.textContent = label;
    if (percentNode) percentNode.textContent = indeterminate ? "" : `%${Math.round(percent)}`;
    if (textNode) textNode.textContent = parts.join(" · ");
  } else if (textNode) {
    // Compact (worker row): one line with label, percent and details.
    textNode.textContent = [label, indeterminate ? "" : `%${Math.round(percent)}`, ...parts].filter(Boolean).join(" · ");
  }
}

function initAdminFilters() {
  document.querySelectorAll("[data-admin-filter]").forEach((input) => {
    const selector = input.dataset.adminFilter;
    if (!selector) return;
    input.addEventListener("input", () => {
      const query = normalizeSearch(input.value);
      document.querySelectorAll(selector).forEach((item) => {
        const haystack = normalizeSearch(item.textContent);
        item.hidden = Boolean(query) && !haystack.includes(query);
      });
      input.dispatchEvent(new CustomEvent("admin-filter-applied"));
    });
  });
}

function initModelContainerRegistrySettings() {
  const form = document.getElementById("model-container-registry-settings-form");
  if (!form) return;
  const saveButton = document.getElementById("btn-save-model-container-registry");
  const testButton = document.getElementById("btn-test-model-container-registry");
  const status = document.getElementById("model-container-registry-form-status");
  const badge = document.getElementById("model-container-registry-status-badge");
  const results = document.getElementById("model-container-registry-test-results");

  const payload = () => {
    const password = String(form.elements.password.value || "");
    return {
      enabled: form.elements.enabled.checked,
      registry_url: String(form.elements.registry_url.value || "").trim(),
      username: String(form.elements.username.value || "").trim(),
      password: form.elements.clear_password.checked ? "" : (password || null),
    };
  };

  const responseError = async (response) => {
    const body = await response.json().catch(() => ({}));
    let detail = body.detail || `Registry isteği başarısız (${response.status})`;
    if (Array.isArray(detail)) detail = detail.map((item) => item.msg).join("; ");
    return new Error(detail);
  };

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    saveButton.disabled = true;
    status.textContent = "Kaydediliyor...";
    status.className = "quota-form-status";
    try {
      const response = await fetch("/api/admin/model-container-registry-settings", {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload()),
      });
      if (!response.ok) throw await responseError(response);
      const saved = await response.json();
      form.elements.password.value = "";
      form.elements.clear_password.checked = false;
      form.elements.password.placeholder = saved.has_password
        ? "Kayıtlı parolayı korumak için boş bırakın"
        : "Registry parolasını girin";
      badge.className = `badge ${saved.enabled ? "badge-running" : "badge-stopped"}`;
      badge.textContent = saved.enabled ? "Etkin" : "Devre Dışı";
      status.textContent = "Model Container Registry ayarları şifreli olarak kaydedildi.";
      status.className = "quota-form-status quota-status-success";
    } catch (error) {
      status.textContent = error.message;
      status.className = "quota-form-status quota-status-error";
    } finally {
      saveButton.disabled = false;
    }
  });

  testButton.addEventListener("click", async () => {
    testButton.disabled = true;
    results.hidden = true;
    results.replaceChildren();
    status.textContent = "Registry controller ve worker'lardan test ediliyor...";
    status.className = "quota-form-status";
    const candidate = payload();
    try {
      const response = await fetch("/api/admin/model-container-registry-settings/test", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          registry_url: candidate.registry_url,
          username: candidate.username,
          password: candidate.password,
        }),
      });
      if (!response.ok) throw await responseError(response);
      const tested = await response.json();
      tested.targets.forEach((target) => {
        const row = document.createElement("div");
        const latency = Number.isInteger(target.latency_ms) ? ` · ${target.latency_ms} ms` : "";
        const httpStatus = target.status_code ? ` · HTTP ${target.status_code}` : "";
        row.textContent = `${target.ok ? "✓" : "✗"} ${target.target_name}${httpStatus}${latency} — ${target.message}`;
        row.className = target.ok ? "quota-status-success" : "quota-status-error";
        results.appendChild(row);
      });
      results.hidden = false;
      status.textContent = tested.ok
        ? "Registry v2 API erişimi controller ve uyumlu build worker'larda doğrulandı."
        : "Registry v2 API erişim testi bir veya daha fazla hedefte başarısız.";
      status.className = `quota-form-status ${tested.ok ? "quota-status-success" : "quota-status-error"}`;
    } catch (error) {
      status.textContent = error.message;
      status.className = "quota-form-status quota-status-error";
    } finally {
      testButton.disabled = false;
    }
  });
}

function initKiloCodeSettings() {
  const form = document.getElementById("kilo-code-settings-form");
  if (!form) return;
  const saveButton = document.getElementById("btn-save-kilo-code");
  const status = document.getElementById("kilo-code-form-status");
  const badge = document.getElementById("kilo-code-status-badge");
  const blocked = document.getElementById("kilo-code-warning");
  const splitList = (value) => String(value || "")
    .split(/[\s,]+/)
    .map((item) => item.trim())
    .filter(Boolean);

  function showState(result) {
    badge.className = `badge ${result.active ? "badge-running" : "badge-stopped"}`;
    badge.textContent = result.active ? "Etkin" : "Devre Dışı";
    blocked.hidden = !result.warning;
    blocked.textContent = result.warning || "";
    form.elements.indexing_key.placeholder = result.has_indexing_key
      ? "Kayıtlı anahtarı korumak için boş bırakın"
      : "İndeksleme API anahtarını girin";
    form.elements.catalog_token.placeholder = result.has_catalog_token
      ? "Kayıtlı token'ı korumak için boş bırakın"
      : "Katalog Bearer token'ı";
  }

  fetch("/api/admin/kilo-code-settings")
    .then((response) => (response.ok ? response.json() : null))
    .then((result) => { if (result) showState(result); })
    .catch(() => {});

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    const key = String(data.get("indexing_key") || "").trim();
    const clearKey = form.elements.clear_indexing_key.checked;
    const token = String(data.get("catalog_token") || "").trim();
    const clearToken = form.elements.clear_catalog_token.checked;
    saveButton.disabled = true;
    status.textContent = "Kaydediliyor...";
    status.className = "quota-form-status";
    try {
      const response = await fetch("/api/admin/kilo-code-settings", {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          enabled: form.elements.enabled.checked,
          catalog_url: String(data.get("catalog_url") || "").trim(),
          catalog_token: clearToken ? "" : (token || null),
          skills_urls: splitList(data.get("skills_urls")),
          model_ids: splitList(data.get("model_ids")),
          indexing_enabled: form.elements.indexing_enabled.checked,
          indexing_model: String(data.get("indexing_model") || "").trim(),
          indexing_dimension: Number.parseInt(String(data.get("indexing_dimension") || "1024"), 10),
          indexing_key: clearKey ? "" : (key || null),
        }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) {
        let detail = result.detail || `Ayarlar kaydedilemedi (${response.status})`;
        if (Array.isArray(detail)) detail = detail.map((item) => item.msg).join("; ");
        throw new Error(detail);
      }
      form.elements.indexing_key.value = "";
      form.elements.clear_indexing_key.checked = false;
      form.elements.catalog_token.value = "";
      form.elements.clear_catalog_token.checked = false;
      form.elements.catalog_url.value = result.catalog_url || "";
      form.elements.skills_urls.value = (result.skills_urls || []).join(", ");
      form.elements.model_ids.value = (result.model_ids || []).join(", ");
      showState(result);
      status.textContent = "Kilo Code ayarları kaydedildi; yeni ve yeniden başlatılan workspace'lere uygulanacak.";
      status.className = "quota-form-status quota-status-success";
    } catch (error) {
      status.textContent = error.message;
      status.className = "quota-form-status quota-status-error";
    } finally {
      saveButton.disabled = false;
    }
  });
}

function initJupyterAiSettings() {
  const form = document.getElementById("jupyter-ai-settings-form");
  if (!form) return;
  const saveButton = document.getElementById("btn-save-jupyter-ai");
  const status = document.getElementById("jupyter-ai-form-status");
  const badge = document.getElementById("jupyter-ai-status-badge");
  const modelList = document.getElementById("jupyter-ai-model-list");
  const defaultModel = form.elements.model_id;
  const addModelButton = document.getElementById("btn-add-jupyter-ai-model");
  const testButton = document.getElementById("btn-test-jupyter-ai");
  const testResults = document.getElementById("jupyter-ai-test-results");

  function models() {
    return Array.from(modelList.querySelectorAll("[data-jupyter-ai-model-row]"))
      .map((row) => ({
        model_id: String(row.querySelector('[data-model-field="model_id"]').value || "").trim(),
        name: String(row.querySelector('[data-model-field="name"]').value || "").trim(),
        description: String(row.querySelector('[data-model-field="description"]').value || "").trim(),
      }))
      .filter((model) => model.model_id || model.name || model.description);
  }

  function syncDefaultModels() {
    const selected = defaultModel.value;
    const catalog = models().filter((model) => model.model_id && model.name);
    defaultModel.innerHTML = catalog.map((model) => {
      const option = document.createElement("option");
      option.value = model.model_id;
      option.textContent = `${model.name} — ${model.model_id}`;
      return option.outerHTML;
    }).join("");
    if (catalog.some((model) => model.model_id === selected)) {
      defaultModel.value = selected;
    }
  }

  let modelRowIndex = modelList.querySelectorAll("[data-jupyter-ai-model-row]").length;

  function addModelRow(model = {}) {
    const row = document.createElement("div");
    const rowKey = `jupyter-model-new-${modelRowIndex++}`;
    row.className = "grid grid-cols-3 jupyter-ai-model-row";
    row.dataset.jupyterAiModelRow = "";
    row.style.cssText = "gap:0.5rem;margin-bottom:0.5rem;align-items:end;";
    row.innerHTML = `
      <div class="form-group" style="margin:0"><label class="form-label" for="${rowKey}-id">Model ID</label><input id="${rowKey}-id" class="form-input" data-model-field="model_id" data-inline-edit-input placeholder="claude-model-alias"></div>
      <div class="form-group" style="margin:0"><label class="form-label" for="${rowKey}-name">Görünen Ad</label><input id="${rowKey}-name" class="form-input" data-model-field="name" data-inline-edit-input placeholder="Model adı"></div>
      <div style="display:flex;gap:0.5rem;align-items:end"><div class="form-group" style="margin:0;flex:1"><label class="form-label" for="${rowKey}-description">Açıklama</label><input id="${rowKey}-description" class="form-input" data-model-field="description" data-inline-edit-input placeholder="On-Prem / Online"></div><button type="button" class="btn btn-danger btn-sm" data-remove-jupyter-ai-model>Sil</button></div>`;
    Object.entries(model).forEach(([key, value]) => {
      const input = row.querySelector(`[data-model-field="${key}"]`);
      if (input) input.value = value;
    });
    const displayName = String(model.name || "Yeni model");
    row.querySelector('[data-model-field="model_id"]').dataset.inlineEditLabel = `${displayName} model kimliğini`;
    row.querySelector('[data-model-field="name"]').dataset.inlineEditLabel = `${displayName} görünen adını`;
    row.querySelector('[data-model-field="description"]').dataset.inlineEditLabel = `${displayName} açıklamasını`;
    row.querySelector("[data-remove-jupyter-ai-model]").setAttribute("aria-label", `${displayName} modelini sil`);
    modelList.appendChild(row);
    initInlineEditControls(row);
    row.querySelector('[data-model-field="model_id"]')._setInlineEditing?.(true);
  }

  modelList.addEventListener("input", syncDefaultModels);
  modelList.addEventListener("click", (event) => {
    const remove = event.target.closest("[data-remove-jupyter-ai-model]");
    if (!remove) return;
    remove.closest("[data-jupyter-ai-model-row]").remove();
    syncDefaultModels();
  });
  addModelButton.addEventListener("click", () => addModelRow());

  testButton.addEventListener("click", async () => {
    testButton.disabled = true;
    testResults.hidden = true;
    testResults.replaceChildren();
    status.textContent = "Seçili model worker'lardan test ediliyor...";
    status.className = "quota-form-status";
    try {
      const response = await fetch("/api/admin/jupyter-ai-settings/test", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({model_id: String(defaultModel.value || "").trim()}),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) {
        let detail = result.detail || `Bağlantı testi çalıştırılamadı (${response.status})`;
        if (Array.isArray(detail)) detail = detail.map((item) => item.msg).join("; ");
        throw new Error(detail);
      }
      result.workers.forEach((worker) => {
        const row = document.createElement("div");
        const latency = Number.isInteger(worker.latency_ms)
          ? ` · ${worker.latency_ms} ms`
          : "";
        const httpStatus = worker.status_code ? ` · HTTP ${worker.status_code}` : "";
        row.textContent = `${worker.ok ? "✓" : "✗"} ${worker.node_name}${httpStatus}${latency} — ${worker.message}`;
        row.className = worker.ok ? "quota-status-success" : "quota-status-error";
        testResults.appendChild(row);
      });
      testResults.hidden = false;
      status.textContent = result.ok
        ? `${result.model_id} tüm etkin worker'larda çalışıyor.`
        : `${result.model_id} testi bir veya daha fazla worker'da başarısız.`;
      status.className = `quota-form-status ${result.ok ? "quota-status-success" : "quota-status-error"}`;
    } catch (error) {
      status.textContent = error.message;
      status.className = "quota-form-status quota-status-error";
    } finally {
      testButton.disabled = false;
    }
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    const token = String(data.get("shared_token") || "");
    const clearToken = form.elements.clear_token.checked;
    const modelCatalog = models();
    saveButton.disabled = true;
    status.textContent = "Kaydediliyor...";
    status.className = "quota-form-status";
    try {
      const response = await fetch("/api/admin/jupyter-ai-settings", {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          enabled: form.elements.enabled.checked,
          gateway_url: String(data.get("gateway_url") || "").trim(),
          model_id: String(data.get("model_id") || "").trim(),
          gateway_model_discovery: form.elements.gateway_model_discovery.checked,
          models: modelCatalog,
          shared_token: clearToken ? "" : (token || null),
          ca_bundle_pem: String(data.get("ca_bundle_pem") || "").trim(),
        }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) {
        let detail = result.detail || `Ayarlar kaydedilemedi (${response.status})`;
        if (Array.isArray(detail)) detail = detail.map((item) => item.msg).join("; ");
        throw new Error(detail);
      }
      form.elements.shared_token.value = "";
      form.elements.clear_token.checked = false;
      form.elements.ca_bundle_pem.value = result.ca_bundle_pem || "";
      form.elements.shared_token.placeholder = result.has_shared_token
        ? "Kayıtlı tokenı korumak için boş bırakın"
        : "Ortak gateway tokenını girin";
      badge.className = `badge ${result.enabled ? "badge-running" : "badge-stopped"}`;
      badge.textContent = result.enabled ? "Etkin" : "Devre Dışı";
      status.textContent = "Workspace AI ayarları kaydedildi; worker'lar en geç 30 saniye içinde alacak.";
      status.className = "quota-form-status quota-status-success";
      closeInlineEditControls(modelList);
    } catch (error) {
      status.textContent = error.message;
      status.className = "quota-form-status quota-status-error";
    } finally {
      saveButton.disabled = false;
    }
  });
}

function initWorkspaceImageManager() {
  const table = document.getElementById("workspace-image-table");
  if (!table) return;
  const tbody = table.querySelector("tbody");
  const count = document.getElementById("workspace-image-count");
  const globalStatus = document.getElementById("workspace-image-global-status");
  const registryForm = document.getElementById("workspace-image-registry-form");
  const uploadForm = document.getElementById("workspace-image-upload-form");
  const registryTemplateSelect = document.getElementById("workspace-image-template-select");
  const newTemplateFields = document.getElementById("workspace-image-new-template-fields");

  const syncRegistryTemplateMode = () => {
    const creating = registryTemplateSelect?.value === "__new__";
    if (newTemplateFields) newTemplateFields.hidden = !creating;
    newTemplateFields?.querySelectorAll("[data-new-template-field]").forEach((field) => {
      field.disabled = !creating;
    });
  };
  registryTemplateSelect?.addEventListener("change", syncRegistryTemplateMode);
  syncRegistryTemplateMode();

  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
  const humanSize = (bytes) => {
    let value = Number(bytes || 0);
    const units = ["B", "KB", "MB", "GB", "TB"];
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
    return `${value.toFixed(unit ? 1 : 0)} ${units[unit]}`;
  };
  const setStatus = (element, message, error = false) => {
    if (!element) return;
    element.textContent = message;
    element.className = `quota-form-status ${error ? "quota-status-error" : "quota-status-success"}`;
  };
  const syncStateLabel = {
    pending: "Bekliyor", queued: "Sırada", downloading: "İndiriliyor",
    verifying: "Doğrulanıyor", loading: "Podman'a yükleniyor",
    ready: "Hazır", failed: "Başarısız", offline: "Worker çevrimdışı",
  };
  const renderWorkerProgress = (workers) => {
    if (!workers?.length) return '<small>Kayıtlı worker yok</small>';
    return `<div class="worker-sync-list">${workers.map((worker) => {
      const percent = Math.max(0, Math.min(100, Number(worker.percent || 0)));
      const error = worker.error ? `<small class="worker-sync-error quota-status-error" title="${escapeHtml(worker.error)}">${escapeHtml(worker.error)}</small>` : "";
      return `<div class="worker-sync-item">
        <div class="worker-sync-heading"><small><strong>${escapeHtml(worker.node_name)}</strong> · ${escapeHtml(syncStateLabel[worker.state] || worker.state)}</small><small>${percent.toFixed(0)}%</small></div>
        <div class="worker-sync-track" role="progressbar" aria-label="${escapeHtml(worker.node_name)} image eşitleme" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent.toFixed(0)}"><div class="worker-sync-fill ${worker.state === "failed" ? "is-failed" : ""}" style="width:${percent}%;"></div></div>
        <small class="worker-sync-size">${humanSize(worker.downloaded_bytes)} / ${humanSize(worker.total_bytes)}</small>${error}
      </div>`;
    }).join("")}</div>`;
  };

  const copyToClipboard = async (value) => {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      return;
    }
    const textarea = document.createElement("textarea");
    textarea.value = value;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.select();
    const copied = document.execCommand("copy");
    textarea.remove();
    if (!copied) throw new Error("Clipboard API unavailable");
  };
  const renderHashValue = (label, value, imageName) => {
    const fullValue = String(value || "-");
    const truncated = fullValue.length > 24 ? `${fullValue.slice(0, 24)}…` : fullValue;
    const copyButton = fullValue === "-" ? "" : `<button type="button" class="copy-value-button" data-copy-value="${escapeHtml(fullValue)}" aria-label="${escapeHtml(imageName)} ${label} değerini kopyala">Kopyala</button>`;
    return `<div class="image-hash-value"><code title="${escapeHtml(fullValue)}">${escapeHtml(truncated)}</code>${copyButton}</div>`;
  };
  let lastRenderedCatalog = null;
  // Outcome of the last action per image, rendered on that image's own row:
  // the global status sits at the top of the page, out of sight of the card.
  const imageMessages = new Map();
  const renderImageMessage = (imageId) => {
    const message = imageMessages.get(imageId);
    if (!message) return "";
    return `<small class="image-action-status quota-form-status ${message.error ? "quota-status-error" : "quota-status-success"}" role="status">${escapeHtml(message.text)}</small>`;
  };

  async function loadImages({ force = false } = {}) {
    try {
      const response = await fetch("/api/admin/workspace-images", { cache: "no-store" });
      const images = await response.json().catch(() => []);
      if (!response.ok) throw new Error(images.detail || `Katalog okunamadı (${response.status})`);
      count.textContent = `${images.length} sürüm`;
      const catalogMarkup = images.length ? images.map((image) => `
        <tr>
          <td data-label="Şablon / Sürüm" class="image-cell-title"><strong>${escapeHtml(image.display_name)}</strong><code>${escapeHtml(image.template_id)}</code><small title="${escapeHtml(image.image_ref)}">${escapeHtml(image.image_ref)}</small></td>
          <td data-label="Kaynak" class="image-cell-source"><span class="badge badge-neutral">${escapeHtml(image.source_type)}</span><small title="${escapeHtml(image.source_ref)}">${escapeHtml(image.source_ref)}</small></td>
          <td data-label="Digest / SHA-256" class="image-cell-hash">${renderHashValue("digest", image.digest, image.display_name)}${renderHashValue("SHA-256", image.sha256, image.display_name)}</td>
          <td data-label="Boyut" class="image-cell-size">${humanSize(image.size)}</td>
          <td data-label="Worker" class="image-cell-worker"><strong>${image.synced_workers} / ${image.total_workers}</strong>${renderWorkerProgress(image.workers)}</td>
          <td data-label="Durum" class="admin-table-status"><span class="badge ${image.enabled ? "badge-running" : "badge-neutral"}">${image.enabled ? "Etkin" : "Devre Dışı"}</span></td>
          <td data-label="İşlemler" class="image-cell-actions"><div class="table-actions admin-table-actions">
            <button type="button" class="btn btn-secondary btn-sm" data-image-toggle="${escapeHtml(image.id)}" data-enabled="${image.enabled}" aria-label="${escapeHtml(image.display_name)} sürümünü ${image.enabled ? "devre dışı bırak" : "etkinleştir"}">${image.enabled ? "Devre Dışı Bırak" : "Etkinleştir"}</button>
            <button type="button" class="btn btn-danger btn-sm" data-image-delete="${escapeHtml(image.id)}" data-image-source="${escapeHtml(image.source_type)}" aria-label="${escapeHtml(image.display_name)} sürümünü sil">Sil</button>
          </div>${renderImageMessage(image.id)}</td>
        </tr>`).join("") : '<tr><td class="table-empty" colspan="7">Henüz workspace image eklenmedi.</td></tr>';
      if ((force || !tbody.contains(document.activeElement)) && catalogMarkup !== lastRenderedCatalog) {
        tbody.innerHTML = catalogMarkup;
        lastRenderedCatalog = catalogMarkup;
      }
    } catch (error) {
      setStatus(globalStatus, error.message, true);
    }
  }

  async function submitForm(form, url, json = false) {
    const button = form.querySelector('button[type="submit"]');
    const status = form.querySelector("[data-image-form-status]");
    button.disabled = true;
    status.textContent = "İçe aktarılıyor; büyük image'lar birkaç dakika sürebilir...";
    status.className = "quota-form-status";
    try {
      let body;
      let createdTemplate = null;
      const options = { method: "POST" };
      if (json) {
        const values = Object.fromEntries(new FormData(form).entries());
        if (values.template_id === "__new__") {
          createdTemplate = {
            id: values.new_template_id,
            name: values.new_template_name,
            description: values.new_template_description || "",
            category: values.new_template_category || "Custom",
            default_port: Number(values.new_template_default_port || 8080),
            ide_type: values.new_template_ide_type || "vscode",
          };
          values.template_id = createdTemplate.id;
          values.new_template = createdTemplate;
          Object.keys(values).filter((key) => key.startsWith("new_template_")).forEach((key) => delete values[key]);
        }
        body = JSON.stringify(values);
        options.headers = { "Content-Type": "application/json" };
      } else {
        body = new FormData(form);
      }
      options.body = body;
      const response = await fetch(url, options);
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || `Image içe aktarılamadı (${response.status})`);
      setStatus(status, "Image doğrulandı, etkinleştirildi ve worker kataloğuna eklendi.");
      form.reset();
      if (createdTemplate) {
        [registryTemplateSelect, uploadForm?.elements.template_id].forEach((select) => {
          if (!select || select.querySelector(`option[value="${CSS.escape(createdTemplate.id)}"]`)) return;
          const option = new Option(`${createdTemplate.name} — ${result.image_ref}`, createdTemplate.id);
          const createOption = select.querySelector('option[value="__new__"]');
          select.insertBefore(option, createOption);
        });
        registryTemplateSelect.value = createdTemplate.id;
      }
      syncRegistryTemplateMode();
      await loadImages({ force: true });
    } catch (error) {
      setStatus(status, error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  registryForm.addEventListener("submit", (event) => {
    event.preventDefault();
    submitForm(registryForm, "/api/admin/workspace-images/import", true);
  });
  uploadForm.addEventListener("submit", (event) => {
    event.preventDefault();
    submitForm(uploadForm, "/api/admin/workspace-images/upload");
  });
  document.getElementById("btn-refresh-workspace-images").addEventListener("click", () => loadImages({ force: true }));
  tbody.addEventListener("click", async (event) => {
    const copy = event.target.closest("[data-copy-value]");
    if (copy) {
      try {
        await copyToClipboard(copy.dataset.copyValue);
        const originalLabel = copy.textContent;
        copy.textContent = "Kopyalandı";
        window.setTimeout(() => { copy.textContent = originalLabel; }, 1500);
      } catch (error) {
        setStatus(globalStatus, "Değer panoya kopyalanamadı.", true);
      }
      return;
    }
    const toggle = event.target.closest("[data-image-toggle]");
    const remove = event.target.closest("[data-image-delete]");
    if (!toggle && !remove) return;
    const action = toggle || remove;
    const row = action.closest("tr");
    const imageId = action.dataset.imageToggle || action.dataset.imageDelete;
    const confirmation = remove?.dataset.imageSource === "mlflow_model"
      ? "Bu model image'ı controller'dan, worker'lardan ve model registry'sinden kalıcı olarak silinsin mi?"
      : "Bu image arşivi controller'dan kalıcı olarak silinsin mi?";
    if (remove && !window.confirm(confirmation)) return;
    imageMessages.delete(imageId);
    action.disabled = true;
    row.setAttribute("aria-busy", "true");
    try {
      const response = await fetch(`/api/admin/workspace-images/${encodeURIComponent(imageId)}`, {
        method: remove ? "DELETE" : "PATCH",
        headers: remove ? {} : { "Content-Type": "application/json" },
        body: remove ? undefined : JSON.stringify({ enabled: toggle.dataset.enabled !== "true" }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || `İşlem başarısız (${response.status})`);
      if (response.status === 202 && result.detail) {
        imageMessages.set(imageId, { text: result.detail, error: false });
      }
    } catch (error) {
      imageMessages.set(imageId, { text: error.message, error: true });
    } finally {
      action.disabled = false;
      row.removeAttribute("aria-busy");
    }
    await loadImages({ force: true });
  });
  loadImages({ force: true });
  window.setInterval(() => loadImages(), 5000);
}

// 1. Workspace Creation Modal with Live Real-Time Deployment Log Streamer
function initWorkspaceCreationModal() {
  const modalBackdrop = document.getElementById("create-modal");
  const openBtn = document.getElementById("btn-open-create-modal");
  const closeBtn = document.getElementById("btn-close-create-modal");
  const form = document.getElementById("create-workspace-form");
  const terminalBox = document.getElementById("deploy-terminal-box");
  const terminal = document.getElementById("deploy-terminal");
  const statusBadge = document.getElementById("deploy-status-badge");
  const modalFooter = document.getElementById("modal-footer-actions");
  const submitBtn = document.getElementById("btn-submit-deploy");
  const cancelBtn = document.getElementById("btn-cancel-deploy");

  if (!modalBackdrop) return;

  function resetDeploymentUi() {
    if (terminalBox) terminalBox.style.display = "none";
    if (terminal) terminal.innerHTML = "";
    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.style.display = "inline-flex";
      submitBtn.innerHTML = "Çalışma Alanını Kur";
    }
    if (cancelBtn) cancelBtn.style.display = "inline-flex";
  }

  function openModal({resetDeploymentUI = true} = {}) {
    const wasOpen = modalBackdrop.classList.contains("open");
    if (resetDeploymentUI) resetDeploymentUi();
    if (!wasOpen) {
      modalBackdrop.classList.add("open");
      modalBackdrop.dispatchEvent(new CustomEvent("aifactory:workspace-modal-opened"));
    }
    return true;
  }

  function closeModal({force = false} = {}) {
    if (!force && submitBtn?.disabled) return false;
    if (!modalBackdrop.classList.contains("open")) return true;
    modalBackdrop.classList.remove("open");
    modalBackdrop.dispatchEvent(new CustomEvent("aifactory:workspace-modal-closed"));
    return true;
  }

  window.AIFactoryWorkspaceCreateModal = {
    open: openModal,
    close: closeModal,
    isOpen: () => modalBackdrop.classList.contains("open"),
  };

  openBtn?.addEventListener("click", () => openModal());
  closeBtn?.addEventListener("click", () => closeModal());
  cancelBtn?.addEventListener("click", () => closeModal());
  modalBackdrop.addEventListener("click", (event) => {
    if (event.target === modalBackdrop) closeModal();
  });
  if (!form) return;

  const templateInputs = form.querySelectorAll('input[name="template_id"]');
  const selectedTemplate = () => form.querySelector('input[name="template_id"]:checked');
  const flavorPicker = document.getElementById("flavor-picker");
  const flavorError = document.getElementById("workspace-flavor-error");
  const nameInput = document.getElementById("input-workspace-name");
  nameInput.addEventListener("input", () => nameInput.setCustomValidity(""));

  // Flavor Selection
  const flavorCards = form.querySelectorAll(".flavor-card");
  const flavorInputs = [...form.querySelectorAll('input[name="flavor_id"]')];
  const flavorSummary = document.getElementById("selected-flavor-summary");
  const cpuFlavorInputs = form.querySelectorAll('.flavor-grid-cpu input[name="flavor_id"]');
  const defaultFlavorInput = form.querySelector('input[name="flavor_id"]:checked');
  let gpuTemplateSelected = false;

  function syncFlavorSelection() {
    const selectedInput = form.querySelector('input[name="flavor_id"]:checked:not(:disabled)');
    flavorCards.forEach((card) => {
      const input = card.querySelector('input[name="flavor_id"]');
      card.classList.toggle("selected", input === selectedInput);
    });
    if (selectedInput) flavorError.hidden = true;
    if (flavorSummary) {
      const selectedCard = selectedInput?.closest(".flavor-card");
      flavorSummary.textContent = selectedCard
        ? "Seçili: " + selectedCard.dataset.flavorSummary
        : gpuTemplateSelected
          ? "Bu şablon GPU profili gerektirir; şu an uygun GPU profili yok"
          : "Şu an kullanılabilir kaynak profili yok";
    }
  }

  // GPU-only templates (e.g. CUDA) cannot run on a CPU flavor: lock the CPU
  // cards and move the selection to the first available GPU profile.
  function applyTemplateFlavorRules() {
    const template = selectedTemplate();
    templateInputs.forEach(input => input.closest(".template-card").classList.toggle("selected", input === template));
    gpuTemplateSelected = template?.closest(".template-card").dataset.requiresGpu === "true";
    cpuFlavorInputs.forEach((input) => {
      input.disabled = gpuTemplateSelected;
      input.closest(".flavor-card")?.classList.toggle("is-unavailable", gpuTemplateSelected);
    });
    const selected = form.querySelector('input[name="flavor_id"]:checked');
    if (!selected || selected.disabled) {
      if (selected) selected.checked = false;
      const next = defaultFlavorInput && !defaultFlavorInput.disabled
        ? defaultFlavorInput
        : flavorInputs.find(input => !input.disabled);
      if (next) next.checked = true;
    }
    syncFlavorSelection();
  }

  flavorInputs.forEach((input) => {
    input.addEventListener("change", syncFlavorSelection);
  });
  templateInputs.forEach(input => input.addEventListener("change", applyTemplateFlavorRules));
  applyTemplateFlavorRules();
  window.addEventListener("pageshow", applyTemplateFlavorRules);

  // Tag placement: GPU slot availability depends on where the workspace may
  // land, so re-check the GPU cards whenever the selected tags change.
  const placementTagInputs = document.querySelectorAll(".input-placement-tag");
  function selectedPlacement() {
    return {
      tags: Array.from(placementTagInputs).filter((input) => input.checked).map((input) => input.value),
    };
  }
  let placementRefresh = 0;
  async function refreshGpuAvailability() {
    const {tags} = selectedPlacement();
    const params = new URLSearchParams();
    tags.forEach((tag) => params.append("tags", tag));
    const request = ++placementRefresh;
    try {
      const response = await fetch(`/api/workspaces/flavors?${params}`, {cache: "no-store"});
      if (!response.ok || request !== placementRefresh) return;
      const catalog = await response.json();
      catalog.forEach((item) => {
        if (!item.accelerator_count) return;
        const card = document.querySelector(`.flavor-card-gpu[data-flavor-id="${CSS.escape(item.id)}"]`);
        if (!card) return;
        const quotaOk = card.dataset.gpuQuotaOk !== "false";
        const available = item.available !== false && quotaOk;
        const input = card.querySelector('input[name="flavor_id"]');
        if (input) {
          input.disabled = !available;
          if (!available && input.checked) input.checked = false;
        }
        card.classList.toggle("is-unavailable", !available);
        const badge = card.querySelector(".gpu-flavor-availability");
        if (badge) {
          badge.className = `badge ${available ? "badge-running" : "badge-error"} gpu-flavor-availability`;
          badge.textContent = available ? `${item.available_slots ?? 0} boş slot` : "Kullanılamıyor";
        }
        const models = card.querySelector(".gpu-flavor-models");
        if (models) {
          models.textContent = item.eligible_accelerator_models?.length
            ? item.eligible_accelerator_models.join(", ")
            : "Uygun GPU worker bekleniyor";
        }
        const note = card.querySelector(".gpu-flavor-unavailable");
        if (note) {
          note.hidden = available;
          note.textContent = available
            ? ""
            : quotaOk
              ? item.availability_message
              : "GPU kotanız yok. Yöneticinizden GPU slot kotası isteyin.";
        }
      });
      applyTemplateFlavorRules();
    } catch (_) {
      // Availability is advisory; the create request reports the real result.
    }
  }
  placementTagInputs.forEach((input) => input.addEventListener("change", refreshGpuAvailability));

  function appendLog(text, level = "info") {
    if (!terminal) return;
    const now = new Date().toTimeString().split(" ")[0];
    const line = document.createElement("div");
    line.className = `log-line log-${level}`;
    line.textContent = `[${now}] ${text}`;
    terminal.appendChild(line);
    terminal.scrollTop = terminal.scrollHeight;
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();

    if (submitBtn?.disabled) return;
    const name = nameInput.value.trim();
    const description = document.getElementById("input-workspace-desc")?.value.trim() || "";
    const templateId = selectedTemplate()?.value;
    const flavorId = form.querySelector('input[name="flavor_id"]:checked:not(:disabled)')?.value;
    const autoStopInput = document.getElementById("input-auto-stop");
    const autoStopMinutes = autoStopInput ? parseInt(autoStopInput.value, 10) || 0 : 0;

    if (name.length < 2) {
      nameInput.setCustomValidity("Çalışma alanı adı en az iki karakter olmalıdır.");
      nameInput.reportValidity();
      return;
    }
    if (!flavorId) {
      flavorError.textContent = gpuTemplateSelected
        ? "Uygun GPU profili yok. Başka bir şablon seçin veya yerleşim etiketlerini kontrol edin."
        : "Kullanılabilir kaynak profili yok. Yöneticinizle iletişime geçin.";
      flavorError.hidden = false;
      flavorPicker.focus();
      return;
    }

    if (terminalBox) terminalBox.style.display = "block";
    if (terminal) terminal.innerHTML = "";
    if (statusBadge) {
      statusBadge.className = "badge badge-creating";
      statusBadge.textContent = "Kuruluyor...";
    }

    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.innerHTML = '<span class="pulse-dot" style="margin-right: 0.35rem;"></span> Kuruluyor...';
    }
    if (cancelBtn) cancelBtn.style.display = "none";

    appendLog(`'${name}' için kurulum süreci başlatılıyor (${templateId}, ${flavorId})...`, "dim");

    try {
      const response = await fetch("/api/workspaces/deploy-stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: name,
          description: description,
          template_id: templateId,
          flavor_id: flavorId,
          auto_stop_minutes: autoStopMinutes,
          ...selectedPlacement(),
        }),
      });

      if (!response.ok) {
        const err = await response.json().catch(() => ({}));
        throw new Error(apiErrorMessage(err, `Çalışma alanı kurulamadı (HTTP ${response.status})`));
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let deploymentCompleted = false;

      function processLine(rawLine) {
        const line = rawLine.trim();
        if (!line || !line.startsWith("data: ")) return;
        try {
          const data = JSON.parse(line.slice(6));
          if (data.type === "log") {
            appendLog(data.text, data.level || "info");
          } else if (data.type === "error") {
            appendLog(data.text || data.error, "error");
            if (statusBadge) {
              statusBadge.className = "badge badge-error";
              statusBadge.textContent = "Hata";
            }
            if (submitBtn) {
              submitBtn.disabled = false;
              submitBtn.innerHTML = "Kurulumu Yeniden Dene";
            }
            if (cancelBtn) cancelBtn.style.display = "inline-flex";
          } else if (data.type === "done") {
            deploymentCompleted = true;
            const ideUrl = new URL(data.web_url || "", window.location.origin);
            if (ideUrl.origin !== window.location.origin ||
                !/^\/proxy\/[0-9a-fA-F-]{36}\/$/.test(ideUrl.pathname)) {
              appendLog("IDE adresi doğrulanamadı. Panele dönülüyor.", "error");
              setTimeout(() => window.location.reload(), 2000);
              return;
            }
            if (statusBadge) {
              statusBadge.className = "badge badge-running";
              statusBadge.textContent = "Hazır";
            }
            appendLog("Container çevrimiçi ve doğrulandı. Çalışma alanı hazır.", "success");

            // The IDE opens in a new tab so this AI Factory tab stays open.
            // Browsers may block the automatic tab after a long deployment;
            // the button then opens it with the user's click.
            const ideTab = window.open(ideUrl.pathname, "_blank");
            if (ideTab) {
              ideTab.opener = null;
              appendLog("IDE yeni sekmede açıldı.", "success");
              setTimeout(() => window.location.reload(), 1500);
            } else if (modalFooter) {
              modalFooter.innerHTML = `
                <button type="button" class="btn btn-secondary" id="btn-workspace-back">Panele Dön</button>
                <a href="${ideUrl.pathname}" target="_blank" rel="noopener" class="btn btn-success" id="btn-workspace-open-ide" style="padding: 0.65rem 1.5rem; font-weight: 600;">
                  <span>↗</span> IDE'yi Yeni Sekmede Aç
                </a>
              `;
              document.getElementById("btn-workspace-back")?.addEventListener("click", () => window.location.reload());
              document.getElementById("btn-workspace-open-ide")?.addEventListener("click", () => {
                setTimeout(() => window.location.reload(), 500);
              });
            }
          }
        } catch (parseErr) {
          console.warn("SSE parse error:", line);
        }
      }

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop();

        for (const line of lines) {
          processLine(line);
        }
      }

      if (buffer) {
        processLine(buffer);
      }

      if (!deploymentCompleted && statusBadge && statusBadge.textContent !== "Hata") {
        setTimeout(() => {
          window.location.reload();
        }, 1000);
      }
    } catch (err) {
      appendLog(err.message, "error");
      if (statusBadge) {
        statusBadge.className = "badge badge-error";
        statusBadge.textContent = "Hata";
      }
      if (submitBtn) {
        submitBtn.disabled = false;
        submitBtn.innerHTML = "Kurulumu Yeniden Dene";
      }
      if (cancelBtn) cancelBtn.style.display = "inline-flex";
    }
  });
}

// 2. Action Buttons (Start, Stop, Delete)
function initActionButtons() {
  document.querySelectorAll("[data-action]").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.preventDefault();
      const action = btn.dataset.action;
      const workspaceId = btn.dataset.workspaceId;

      if (action === "delete") {
        if (!confirm("Bu çalışma alanını kalıcı olarak silmek istediğinizden emin misiniz? Container ve kalıcı depolamadaki tüm dosyalar silinecektir.")) {
          return;
        }
      }

      btn.disabled = true;
      const originalHtml = btn.innerHTML;
      btn.innerHTML = "...";

      try {
        let url = `/api/workspaces/${workspaceId}`;
        let method = "GET";

        if (action === "start") {
          url += "/start";
          method = "POST";
        } else if (action === "stop") {
          url += "/stop";
          method = "POST";
        } else if (action === "delete") {
          method = "DELETE";
        }

        const res = await fetch(url, { method: method });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          alert(err.detail || `${action} işlemi başarısız.`);
          btn.disabled = false;
          btn.innerHTML = originalHtml;
          return;
        }

        if (action === "delete" && window.location.pathname.startsWith("/workspaces/")) {
          window.location.href = "/";
          return;
        }

        window.location.reload();
      } catch (err) {
        alert("İşlem hatası: " + err.message);
        btn.disabled = false;
        btn.innerHTML = originalHtml;
      }
    });
  });
}

// 3. Live Log Poller for Workspace Detail page
function initLogPolling() {
  const logTerminal = document.getElementById("workspace-logs-output");
  if (!logTerminal) return;

  const workspaceId = logTerminal.dataset.workspaceId;
  if (!workspaceId) return;

  async function fetchLogs() {
    try {
      const res = await fetch(`/api/workspaces/${workspaceId}/logs?tail=150`);
      if (res.ok) {
        const data = await res.json();
        logTerminal.textContent = data.logs || "Henüz log yok.";
      }
    } catch (e) {
      console.warn("Container logları alınamadı:", e);
    }
  }

  fetchLogs();
  setInterval(fetchLogs, 3000);
}

// 4. Admin quota editors: per-user overrides and per-team (group) quotas.
// A blank field means "inherit" (team value for users, default for teams).
function readQuotaFields(form, { allowBlank }) {
  const fields = [
    ["cpu_quota", "cpu_quota", 1, false],
    ["memory_gb_quota", "memory_mb_quota", 1024, true],
    ["disk_gb_quota", "disk_mb_quota", 1024, true],
    ["gpu_quota", "gpu_quota", 1, true],
  ];
  const payload = {};
  for (const [inputName, apiName, factor, integer] of fields) {
    const raw = form.elements[inputName].value.trim();
    if (raw === "") {
      if (!allowBlank) throw new Error("Tüm alanları doldurun.");
      payload[apiName] = null;
      continue;
    }
    const value = Number(raw);
    if (!Number.isFinite(value) || value < 0) throw new Error("Geçerli sayılar girin.");
    payload[apiName] = integer ? Math.round(value * factor) : value * factor;
  }
  return payload;
}

async function submitQuotaRequest(form, url, method, payload) {
  const buttons = form.querySelectorAll("button");
  const status = form.querySelector(".quota-form-status");
  buttons.forEach((button) => { button.disabled = true; });
  status.textContent = "Kaydediliyor...";
  status.className = "quota-form-status";
  try {
    const response = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: payload === undefined ? undefined : JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = Array.isArray(data.detail) ? data.detail.map((item) => item.msg).join(" ") : data.detail;
      throw new Error(detail || `Kota güncellenemedi (${response.status})`);
    }
    status.textContent = "Kaydedildi";
    status.className = "quota-form-status quota-status-success";
    setTimeout(() => window.location.reload(), 650);
  } catch (error) {
    status.textContent = error.message;
    status.className = "quota-form-status quota-status-error";
    buttons.forEach((button) => { button.disabled = false; });
  }
}

function showQuotaError(form, message) {
  const status = form.querySelector(".quota-form-status");
  status.textContent = message;
  status.className = "quota-form-status quota-status-error";
}

function initQuotaForms() {
  // Compact user rows keep their quota editor folded until "Kota" is pressed.
  document.querySelectorAll("[data-quota-toggle]").forEach((button) => {
    const form = button.closest(".admin-user-card")?.querySelector(".quota-form");
    if (!form) return;
    button.addEventListener("click", () => {
      form.hidden = !form.hidden;
      button.setAttribute("aria-expanded", String(!form.hidden));
      button.classList.toggle("active", !form.hidden);
      if (!form.hidden) form.querySelector("input")?.focus();
    });
  });

  document.querySelectorAll(".quota-form[data-user-id]").forEach((form) => {
    const url = `/api/admin/users/${form.dataset.userId}/quota`;
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      let payload;
      try { payload = readQuotaFields(form, { allowBlank: true }); }
      catch (error) { showQuotaError(form, error.message); return; }
      submitQuotaRequest(form, url, "PUT", payload);
    });
    form.querySelector("[data-quota-reset]")?.addEventListener("click", () => {
      submitQuotaRequest(form, url, "PUT", {
        cpu_quota: null, memory_mb_quota: null, disk_mb_quota: null, gpu_quota: null,
      });
    });
  });

  document.querySelectorAll(".group-quota-form").forEach((form) => {
    const isDefault = form.dataset.defaultGroup === "true";
    const isUnit = form.dataset.kind === "unit";
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      let payload;
      try { payload = readQuotaFields(form, { allowBlank: !isDefault }); }
      catch (error) { showQuotaError(form, error.message); return; }
      const target = isUnit ? { unit: form.dataset.name } : { team: form.dataset.name };
      submitQuotaRequest(form, "/api/admin/user-groups", "PUT", { ...target, ...payload });
    });
    form.querySelector("[data-group-quota-delete]")?.addEventListener("click", () => {
      const message = isUnit
        ? "Müdürlük kotası kaldırılsın mı? Kendi kotası olmayan takımlar varsayılan kotaya döner (takım ve özel kotalar korunur)."
        : "Takım kotası kaldırılsın mı? Üyeler müdürlük veya varsayılan kotaya döner (özel kotalar korunur).";
      if (!confirm(message)) return;
      submitQuotaRequest(form, `/api/admin/user-groups/${form.dataset.groupId}`, "DELETE");
    });
  });
}

// Team groups follow the user search: groups with matches open, others hide.
// A query matching a team name shows all of its members; müdürlüks follow
// their teams.
function initUserGroupFilter() {
  document.querySelectorAll("[data-admin-group-filter]").forEach((input) => {
    const groups = document.querySelectorAll(input.dataset.adminGroupFilter);
    const units = input.dataset.adminUnitFilter ? document.querySelectorAll(input.dataset.adminUnitFilter) : [];
    const sections = input.dataset.adminSectionFilter ? document.querySelectorAll(input.dataset.adminSectionFilter) : [];
    const userOpened = new WeakMap();
    [...groups, ...units].forEach((group) => {
      group.addEventListener("toggle", () => {
        if (!input.value.trim()) userOpened.set(group, group.open);
      });
    });
    input.addEventListener("admin-filter-applied", () => {
      const query = input.value.trim().toLocaleLowerCase("tr-TR");
      groups.forEach((group) => {
        const cards = group.querySelectorAll(".admin-user-card");
        if (!query) {
          group.hidden = false;
          cards.forEach((card) => { card.hidden = false; });
          group.open = Boolean(userOpened.get(group));
          return;
        }
        const name = group.querySelector(":scope > summary").textContent.toLocaleLowerCase("tr-TR");
        if (name.includes(query)) {
          cards.forEach((card) => { card.hidden = false; });
          group.hidden = false;
          group.open = cards.length > 0;
          return;
        }
        const matches = Array.from(cards).some((card) => !card.hidden);
        group.hidden = !matches;
        group.open = matches;
      });
      // Müdürlüks wrap their teams: a müdürlük name match shows every team in
      // it, otherwise the müdürlük stays visible while any of its teams is.
      units.forEach((unit) => {
        const teams = unit.querySelectorAll(input.dataset.adminGroupFilter);
        if (!query) {
          unit.hidden = false;
          unit.open = Boolean(userOpened.get(unit));
          return;
        }
        const name = unit.querySelector(":scope > summary").textContent.toLocaleLowerCase("tr-TR");
        if (name.includes(query)) {
          teams.forEach((team) => {
            team.hidden = false;
            team.querySelectorAll(".admin-user-card").forEach((card) => { card.hidden = false; });
          });
          unit.hidden = false;
          unit.open = true;
          return;
        }
        const matches = Array.from(teams).some((team) => !team.hidden);
        unit.hidden = !matches;
        unit.open = matches;
      });
      // A section (Müdürlükler, a Genel Müdürlük, unresolved teams) hides with its rows.
      sections.forEach((section) => {
        const rows = section.querySelectorAll(":scope > details");
        section.hidden = Boolean(query) && Array.from(rows).every((row) => row.hidden);
      });
    });
  });
}

// Bulk AD sync: places every AD team under its müdürlük and refreshes users.
function initDirectorySync() {
  const button = document.getElementById("btn-directory-sync");
  const status = document.getElementById("directory-sync-status");
  document.querySelectorAll("time[data-local-time]").forEach((node) => {
    const date = new Date(node.dataset.localTime);
    if (!Number.isNaN(date.getTime())) {
      node.textContent = date.toLocaleString("tr-TR", { dateStyle: "short", timeStyle: "short" });
    }
  });
  if (!button || !status) return;
  button.addEventListener("click", async () => {
    button.disabled = true;
    status.className = "tree-sync is-busy";
    status.textContent = "Active Directory okunuyor...";
    try {
      const response = await fetch("/api/admin/directory-sync", { method: "POST" });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || `Senkronizasyon başarısız (${response.status})`);
      status.className = "tree-sync";
      status.textContent = `${data.ad_people} kişi · ${data.teams} takım · ${data.units} müdürlük okundu; ` +
        `${data.updated_users} kullanıcı güncellendi. Sayfa yenileniyor...`;
      setTimeout(() => window.location.reload(), 900);
    } catch (error) {
      status.className = "tree-sync is-error";
      status.textContent = error.message;
      button.disabled = false;
    }
  });
}

// 5. Admin LDAP / Active Directory settings
function initDirectorySettings() {
  const form = document.getElementById("directory-settings-form");
  if (!form) return;

  const testButton = document.getElementById("btn-test-directory");
  const saveButton = document.getElementById("btn-save-directory");
  const status = document.getElementById("directory-form-status");
  const badge = document.getElementById("directory-status-badge");

  function payload() {
    const data = new FormData(form);
    const password = String(data.get("bind_password") || "");
    return {
      enabled: form.elements.enabled.checked,
      server_host: String(data.get("server_host") || "").trim(),
      server_port: Number(data.get("server_port")),
      use_ssl: form.elements.use_ssl.checked,
      validate_tls: form.elements.validate_tls.checked,
      ca_cert_file: String(data.get("ca_cert_file") || "").trim(),
      connect_timeout_seconds: Number(data.get("connect_timeout_seconds")),
      bind_dn: String(data.get("bind_dn") || "").trim(),
      bind_password: password || null,
      user_base_dn: String(data.get("user_base_dn") || "").trim(),
      user_filter: String(data.get("user_filter") || "").trim(),
      username_attribute: String(data.get("username_attribute") || "").trim(),
      email_attribute: String(data.get("email_attribute") || "").trim(),
      display_name_attribute: String(data.get("display_name_attribute") || "").trim(),
      team_attribute: String(data.get("team_attribute") || "").trim(),
      directorate_attribute: String(data.get("directorate_attribute") || "").trim(),
      organization_unit_attribute: String(data.get("organization_unit_attribute") || "").trim(),
      manager_attribute: String(data.get("manager_attribute") || "").trim(),
      title_attribute: String(data.get("title_attribute") || "").trim(),
      unit_head_titles: String(data.get("unit_head_titles") || "").trim(),
      division_head_titles: String(data.get("division_head_titles") || "").trim(),
      group_membership_attribute: String(data.get("group_membership_attribute") || "").trim(),
      required_group_dn: String(data.get("required_group_dn") || "").trim(),
      admin_group_dn: String(data.get("admin_group_dn") || "").trim(),
      nested_group_search: form.elements.nested_group_search.checked,
    };
  }

  function setStatus(message, isError = false) {
    status.textContent = message;
    status.className = `quota-form-status ${isError ? "quota-status-error" : "quota-status-success"}`;
  }

  async function send(url, method) {
    const response = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload()),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      let detail = data.detail || `İşlem başarısız (${response.status})`;
      if (Array.isArray(detail)) detail = detail.map((item) => item.msg).join("; ");
      throw new Error(detail);
    }
    return data;
  }

  testButton?.addEventListener("click", async () => {
    testButton.disabled = true;
    saveButton.disabled = true;
    status.textContent = "LDAPS bağlantısı test ediliyor...";
    status.className = "quota-form-status";
    try {
      const result = await send("/api/admin/directory-settings/test", "POST");
      setStatus(`${result.message} ${result.server} · ${result.response_time_ms} ms`);
    } catch (error) {
      setStatus(error.message, true);
    } finally {
      testButton.disabled = false;
      saveButton.disabled = false;
    }
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    testButton.disabled = true;
    saveButton.disabled = true;
    status.textContent = "Kaydediliyor...";
    status.className = "quota-form-status";
    try {
      const result = await send("/api/admin/directory-settings", "PUT");
      form.elements.bind_password.value = "";
      form.elements.bind_password.placeholder = "Kayıtlı parolayı korumak için boş bırakın";
      badge.className = `badge ${result.enabled ? "badge-running" : "badge-stopped"}`;
      badge.textContent = result.enabled ? "Etkin" : "Devre Dışı";
      setStatus("LDAP ayarları kaydedildi.");
    } catch (error) {
      setStatus(error.message, true);
    } finally {
      testButton.disabled = false;
      saveButton.disabled = false;
    }
  });
}

function initNodeManagement() {
  const tokenBox = document.getElementById("node-enrollment-token");
  const status = document.getElementById("node-create-status");
  const ticketForm = document.getElementById("worker-bootstrap-ticket-form");
  const ticketBox = document.getElementById("worker-bootstrap-ticket-box");
  const ticketStatus = document.getElementById("worker-bootstrap-ticket-status");

  function renderWorkerTokenBox(data) {
    if (!tokenBox) return;
    const controllerUrl = window.location.origin;

    tokenBox.style.display = "block";
    tokenBox.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.75rem;border-bottom:1px solid rgba(255,255,255,0.1);padding-bottom:0.5rem;">
        <strong style="color:var(--text-main,#fff);font-size:0.95rem;">✨ Worker Token / Bağlantı Bilgisi: ${data.name}</strong>
        <span style="font-size:0.75rem;color:var(--text-muted,#9ca3af);">Token yalnızca şimdi gösterilir</span>
      </div>
      <div style="font-size:0.8rem;color:var(--text-muted,#9ca3af);margin-top:0.75rem;line-height:1.5;">
        <div><strong>Node ID:</strong> <code style="color:#e2e8f0;">${data.id}</code></div>
        <div><strong>Token:</strong> <code style="color:#e2e8f0;">${data.enrollment_token}</code></div>
        <div style="margin-top:0.5rem;font-size:0.75rem;">
          <strong>Manuel Yapılandırma (/etc/aifactory/worker.env):</strong>
          <pre style="background:rgba(0,0,0,0.25);padding:0.5rem;border-radius:4px;margin-top:0.25rem;font-size:0.75rem;color:#e2e8f0;white-space:pre;">AIFACTORY_CONTROLLER_URL=${controllerUrl}
AIFACTORY_NODE_ID=${data.id}
AIFACTORY_NODE_TOKEN=${data.enrollment_token}</pre>
        </div>
      </div>
    `;
  }

  function renderWorkerBootstrapTicket(data) {
    if (!ticketBox) return;
    ticketBox.style.display = "block";
    ticketBox.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;gap:.75rem;margin-bottom:.75rem;border-bottom:1px solid rgba(255,255,255,.1);padding-bottom:.5rem;flex-wrap:wrap;">
        <strong style="color:var(--text-main,#fff);font-size:.95rem;">Worker Kurulum Komutu</strong>
        <span id="worker-bootstrap-expiry" style="font-size:.75rem;color:var(--text-muted,#9ca3af);"></span>
      </div>
      <div style="display:flex;justify-content:space-between;align-items:center;gap:.5rem;margin-bottom:.35rem;">
        <span style="font-size:.85rem;font-weight:600;color:var(--accent,#38bdf8);">Yeni worker üzerinde root olarak çalıştırın:</span>
        <button type="button" class="btn btn-secondary btn-sm" id="btn-copy-bootstrap-ticket" style="padding:.25rem .65rem;font-size:.75rem;">Kopyala</button>
      </div>
      <pre id="worker-bootstrap-ticket-command" style="background:rgba(0,0,0,.4);padding:.65rem;border-radius:4px;overflow-x:auto;margin:0;font-size:.8rem;white-space:pre-wrap;overflow-wrap:anywhere;color:#a7f3d0;"></pre>
      <p style="margin:.65rem 0 0;font-size:.76rem;color:var(--text-muted,#9ca3af);">Komut yalnızca bir kez kullanılabilir. Script worker adını sorar ve node/token oluşturma işlemini otomatik tamamlar.</p>
    `;
    const command = document.getElementById("worker-bootstrap-ticket-command");
    const expiry = document.getElementById("worker-bootstrap-expiry");
    const copyButton = document.getElementById("btn-copy-bootstrap-ticket");
    if (command) command.textContent = data.command;
    if (expiry) {
      const expiresAt = new Date(data.expires_at);
      expiry.textContent = `Geçerlilik: ${expiresAt.toLocaleString()}`;
    }
    if (copyButton) {
      copyButton.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(data.command);
          copyButton.textContent = "Kopyalandı! ✓";
        } catch (_) {
          const textarea = document.createElement("textarea");
          textarea.value = data.command;
          document.body.appendChild(textarea);
          textarea.select();
          document.execCommand("copy");
          textarea.remove();
          copyButton.textContent = "Kopyalandı! ✓";
        }
        setTimeout(() => { copyButton.textContent = "Kopyala"; }, 2000);
      });
    }
  }

  if (ticketForm) {
    ticketForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = ticketForm.querySelector("button[type=submit]");
      button.disabled = true;
      if (ticketStatus) {
        ticketStatus.textContent = "Tek kullanımlık komut oluşturuluyor...";
        ticketStatus.className = "quota-form-status";
      }
      try {
        const response = await fetch("/api/admin/worker-bootstrap-tickets", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(data.detail || `Kurulum komutu oluşturulamadı (${response.status})`);
        }
        renderWorkerBootstrapTicket(data);
        if (ticketStatus) {
          ticketStatus.textContent = "Komut hazır. Yeni worker üzerinde çalıştırın.";
          ticketStatus.className = "quota-form-status quota-status-success";
        }
      } catch (error) {
        if (ticketStatus) {
          ticketStatus.textContent = error.message;
          ticketStatus.className = "quota-form-status quota-status-error";
        }
      } finally {
        button.disabled = false;
      }
    });
  }

  // Real-time SSE Stream for Nodes Telemetry and Status
  const renderWorkerUpgradeState = (row, upgrade = {}) => {
    const state = upgrade.state || "idle";
    const labels = {
      idle: "Hazır",
      preparing: "Hazırlanıyor",
      downloading: "İndiriliyor",
      queued: "Kuyrukta",
      running: "Güncelleniyor",
      succeeded: "Güncel",
      failed: "Güncelleme hatası",
    };
    const badge = row?.querySelector(".node-upgrade-state");
    if (!badge) return;
    const badgeClass = state === "succeeded"
      ? "badge-running"
      : state === "failed"
        ? "badge-error"
        : ["preparing", "downloading", "queued", "running"].includes(state)
          ? "badge-starting"
          : "badge-neutral";
    const target = upgrade.target_version ? ` · v${upgrade.target_version}` : "";
    badge.className = `node-upgrade-state badge ${badgeClass}`;
    badge.dataset.state = state;
    badge.textContent = `${labels[state] || state}${target}`;
    badge.title = upgrade.message || "";
    renderUpdateProgress(row?.querySelector("[data-update-progress]"), state, upgrade.progress);
    const detail = row?.querySelector(".node-upgrade-detail");
    if (detail) {
      const timestamp = upgrade.updated_at
        ? new Date(upgrade.updated_at).toLocaleString("tr-TR")
        : "";
      const message = upgrade.message || "";
      detail.textContent = [message, timestamp].filter(Boolean).join(" · ");
      detail.className = `node-upgrade-detail${message ? "" : " is-hidden"}${state === "failed" ? " is-error" : ""}`;
    }
  };

  const nodesTable = document.getElementById("admin-nodes-table");
  if (nodesTable) {
    nodesTable.querySelectorAll("[data-update-progress][data-upgrade]").forEach((box) => {
      try {
        const upgrade = JSON.parse(box.dataset.upgrade || "{}");
        renderUpdateProgress(box, upgrade.state || "idle", upgrade.progress);
      } catch (_error) { /* malformed initial state; SSE will refresh it */ }
    });
    const renderWorkerGpu = (row, accelerators = [], runtimes = {}) => {
      const runtime = runtimes?.nvidia || {};
      const physical = accelerators.filter(device => device?.kind === "physical");
      const mig = accelerators.filter(device => device?.kind === "mig");
      const main = row.querySelector(".node-gpu-main");
      const detail = row.querySelector(".node-gpu-detail");
      if (main) {
        const badge = document.createElement("span");
        if (runtime.status === "ready") {
          badge.className = "badge badge-running";
          badge.textContent = physical.length + " NVIDIA";
        } else if (runtime.status === "error") {
          badge.className = "badge badge-error";
          badge.textContent = "GPU Hatası";
        } else {
          badge.className = "badge badge-neutral";
          badge.textContent = "CPU";
        }
        main.replaceChildren(badge);
      }
      if (detail) {
        if (physical.length) {
          const model = physical[0].model || "NVIDIA GPU";
          const totalMb = physical.reduce((sum, device) => sum + Number(device.memory_mb || 0), 0);
          const usedMb = physical.reduce((sum, device) => sum + Number(device.memory_used_mb || 0), 0);
          const maxUtilization = physical.reduce(
            (highest, device) => Math.max(highest, Number(device.utilization_percent || 0)),
            0
          );
          detail.textContent = model
            + (physical.length > 1 ? " × " + physical.length : "")
            + " · " + (usedMb / 1024).toFixed(1) + "/" + (totalMb / 1024).toFixed(1) + " GB"
            + " · " + maxUtilization.toFixed(0) + "%"
            + (mig.length ? " · " + mig.length + " MIG" : "");
        } else {
          detail.textContent = runtime.status === "error"
            ? (runtime.message || "GPU runtime hazır değil")
            : "GPU yok";
        }
        detail.title = runtime.message || "";
      }
    };

    try {
      const eventSource = new EventSource("/api/admin/nodes/events-stream");
      eventSource.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === "node.connected" || msg.type === "node.disconnected") {
            const row = document.querySelector(`tr[data-node-id="${msg.data.node_id}"]`);
            if (row) {
              const badge = row.querySelector(".node-status-badge");
              if (badge) {
                const isOnline = msg.data.status === "online";
                badge.className = `badge ${isOnline ? "badge-running" : "badge-stopped"} node-status-badge`;
                badge.textContent = (msg.data.status || "offline").toUpperCase();
              }
            }
          } else if (msg.type === "node.telemetry") {
            const d = msg.data;
            const row = document.querySelector(`tr[data-node-id="${d.node_id}"]`);
            if (row) {
              const badge = row.querySelector(".node-status-badge");
              if (badge) {
                const isOnline = d.status === "online";
                badge.className = `badge ${isOnline ? "badge-running" : "badge-stopped"} node-status-badge`;
                badge.textContent = (d.status || "offline").toUpperCase();
              }
              const cpuText = row.querySelector(".node-cpu-text");
              const cpuBar = row.querySelector(".node-cpu-bar");
              if (cpuText) cpuText.innerHTML = `${d.cpu_percent}% <span>(${d.cpu_total}c)</span>`;
              if (cpuBar) cpuBar.style.width = `${Math.min(d.cpu_percent, 100)}%`;

              const ramText = row.querySelector(".node-ram-text");
              const ramBar = row.querySelector(".node-ram-bar");
              const usedGb = (d.memory_used_mb / 1024).toFixed(1);
              const totalGb = (d.memory_total_mb / 1024).toFixed(1);
              const ramPct = d.memory_total_mb ? Math.min((d.memory_used_mb / d.memory_total_mb) * 100, 100) : 0;
              if (ramText) ramText.innerHTML = `${usedGb}<span>/${totalGb}G</span>`;
              if (ramBar) ramBar.style.width = `${ramPct}%`;

              const diskCell = row.querySelector(".node-disk-cell span");
              if (diskCell) {
                const dUsedGb = (d.disk_used_mb / 1024).toFixed(1);
                diskCell.textContent = `${dUsedGb} GB`;
              }

              const cntBadge = row.querySelector(".node-container-count");
              if (cntBadge) cntBadge.textContent = `${d.active_containers_count} cnt`;

              const versionBadge = row.querySelector(".node-version");
              if (versionBadge) {
                versionBadge.textContent = d.agent_version ? `v${d.agent_version}` : "Sürüm bekleniyor";
              }
              if (d.upgrade_status && Object.keys(d.upgrade_status).length) {
                renderWorkerUpgradeState(row, d.upgrade_status);
              }
              renderWorkerGpu(row, d.accelerators || [], d.accelerator_runtime || {});

              const lastSeenCell = row.querySelector(".node-last-seen-cell");
              if (lastSeenCell && d.last_seen_at) {
                lastSeenCell.textContent = d.last_seen_at.replace("T", " ").substring(0, 19);
              }
            }
          }
        } catch (_) {}
      };
    } catch (_) {}
  }

  document.querySelectorAll(".node-toggle-schedule").forEach(button => {
    button.addEventListener("click", async () => {
      const row = button.closest("tr");
      const schedulable = button.dataset.schedulable === "true";
      button.disabled = true;
      try {
        const response = await fetch(`/api/admin/nodes/${row.dataset.nodeId}`, {
          method: "PATCH",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({schedulable: !schedulable}),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || "Worker güncellenemedi.");
        window.location.reload();
      } catch (error) {
        if (status) {
          status.textContent = error.message;
          status.className = "quota-form-status quota-status-error";
        }
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll(".node-gpu-slots").forEach(select => {
    select.addEventListener("change", async () => {
      const row = select.closest("tr");
      select.disabled = true;
      try {
        const response = await fetch(`/api/admin/nodes/${row.dataset.nodeId}`, {
          method: "PATCH",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({gpu_slots_per_device: Number(select.value)}),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || "GPU paylaşım politikası güncellenemedi.");
        window.location.reload();
      } catch (error) {
        alert(error.message);
        select.disabled = false;
      }
    });
  });

  document.querySelectorAll(".node-rotate-token-btn").forEach(button => {
    button.addEventListener("click", async () => {
      const row = button.closest("tr");
      const nodeId = button.dataset.nodeId || row.dataset.nodeId;
      const nodeName = button.dataset.nodeName || "worker";
      if (!confirm(`"${nodeName}" worker node'unun token'ını yenilemek istediğinizden emin misiniz? Eski token ile bağlı olan agent bağlantısı kesilecektir.`)) {
        return;
      }
      button.disabled = true;
      try {
        const response = await fetch(`/api/admin/nodes/${nodeId}/rotate-token`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || "Token yenilenemedi.");
        renderWorkerTokenBox(data);
      } catch (error) {
        alert(error.message);
      } finally {
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll(".node-upgrade-btn").forEach(button => {
    button.addEventListener("click", async () => {
      const row = button.closest("tr");
      const nodeId = button.dataset.nodeId || row.dataset.nodeId;
      const nodeName = button.dataset.nodeName || "worker";
      button.disabled = true;
      button.textContent = "Kontrol ediliyor...";
      try {
        const checkResponse = await fetch(`/api/admin/nodes/${nodeId}/upgrade-check`, {
          cache: "no-store",
        });
        const check = await checkResponse.json().catch(() => ({}));
        if (!checkResponse.ok) throw new Error(check.detail || "Worker sürümü kontrol edilemedi.");
        const comparison = `Kurulu v${check.installed_version} · Yayınlanan v${check.published_version}`;
        if (!check.update_available) {
          renderWorkerUpgradeState(row, {
            state: "succeeded",
            target_version: check.published_version,
            message: `${comparison} · Worker güncel.`,
            updated_at: new Date().toISOString(),
          });
          return;
        }
        if (!check.connected) {
          throw new Error(`${comparison} · Worker çevrimdışı; güncelleme başlatılamaz.`);
        }
        if (!confirm(`${nodeName}: ${comparison}. Güncelleme başlatılsın mı?`)) return;
        button.textContent = "İletiliyor...";
        const response = await fetch(`/api/admin/nodes/${nodeId}/upgrade`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || "Yükseltme başlatılamadı.");
        const result = data.detail || {};
        renderWorkerUpgradeState(row, {
          state: result.status === "already_current" ? "succeeded" : "preparing",
          target_version: result.target_version || "",
          message: result.message || data.message,
          updated_at: new Date().toISOString(),
        });
      } catch (error) {
        renderWorkerUpgradeState(row, {
          state: "failed",
          message: error.message,
        });
        alert(error.message);
      } finally {
        button.disabled = false;
        button.textContent = "Güncelle";
      }
    });
  });

  document.querySelectorAll(".node-delete-btn").forEach(button => {
    button.addEventListener("click", async () => {
      const row = button.closest("tr");
      const nodeId = button.dataset.nodeId || row.dataset.nodeId;
      const nodeName = button.dataset.nodeName || "worker";
      if (!confirm(`"${nodeName}" worker node'unu silmek istediğinizden emin misiniz?`)) {
        return;
      }
      button.disabled = true;
      try {
        const response = await fetch(`/api/admin/nodes/${nodeId}`, {
          method: "DELETE",
          headers: {"Content-Type": "application/json"},
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(data.detail || `Worker silinemedi (${response.status})`);
        }
        window.location.reload();
      } catch (error) {
        alert(error.message);
        button.disabled = false;
      }
    });
  });

  // Node Labels Edit Modal
  const labelModal = document.getElementById("node-label-modal");
  const labelForm = document.getElementById("node-label-form");
  const labelInput = document.getElementById("label-modal-text");
  const labelNodeIdInput = document.getElementById("label-modal-node-id");
  const labelCloseBtn = document.getElementById("btn-close-label-modal");

  if (labelCloseBtn && labelModal) {
    labelCloseBtn.addEventListener("click", () => labelModal.classList.remove("open"));
  }

  document.querySelectorAll(".btn-edit-labels").forEach(button => {
    button.addEventListener("click", () => {
      const nodeId = button.dataset.nodeId;
      let rawLabels = button.dataset.labels || "{}";
      try {
        const parsed = JSON.parse(rawLabels);
        labelInput.value = JSON.stringify(parsed, null, 2);
      } catch (_) {
        labelInput.value = rawLabels;
      }
      labelNodeIdInput.value = nodeId;
      labelModal.classList.add("open");
    });
  });

  if (labelForm) {
    labelForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const nodeId = labelNodeIdInput.value;
      let labelsObj = {};
      const val = labelInput.value.trim();
      if (val) {
        if (val.startsWith("{")) {
          try {
            labelsObj = JSON.parse(val);
          } catch (err) {
            alert("Geçersiz JSON formatı: " + err.message);
            return;
          }
        } else {
          val.split("\n").forEach(line => {
            const [k, ...v] = line.split("=");
            if (k && v.length) labelsObj[k.trim()] = v.join("=").trim();
          });
        }
      }
      try {
        const response = await fetch(`/api/admin/nodes/${nodeId}/labels`, {
          method: "PUT",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({labels: labelsObj}),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || "Etiketler kaydedilemedi.");
        labelModal.classList.remove("open");
        window.location.reload();
      } catch (err) {
        alert(err.message);
      }
    });
  }
}

function apiErrorMessage(data, fallback) {
  const detail = data && data.detail;
  if (Array.isArray(detail)) return detail.map((item) => item.msg).join("; ") || fallback;
  return detail || fallback;
}

// Worker tag assignment and the admin-managed tag catalog.
function initPlacementAdmin() {
  const modal = document.getElementById("node-placement-modal");
  const form = document.getElementById("node-placement-form");
  const nodeIdInput = document.getElementById("placement-modal-node-id");
  const title = document.getElementById("node-placement-modal-title");
  document.getElementById("btn-close-placement-modal")?.addEventListener("click", () => {
    modal?.classList.remove("open");
  });

  document.querySelectorAll(".btn-edit-placement").forEach((button) => {
    button.addEventListener("click", () => {
      if (!modal) return;
      let tags = [];
      try {
        tags = JSON.parse(button.dataset.tags || "[]");
      } catch (_) {
        tags = [];
      }
      nodeIdInput.value = button.dataset.nodeId;
      if (title) title.textContent = `Worker Yerleşimi · ${button.dataset.nodeName || ""}`;
      modal.querySelectorAll(".placement-modal-tag").forEach((box) => {
        box.checked = tags.includes(box.value);
      });
      modal.classList.add("open");
    });
  });

  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const tags = Array.from(form.querySelectorAll(".placement-modal-tag:checked")).map((box) => box.value);
    try {
      const response = await fetch(`/api/admin/nodes/${nodeIdInput.value}/placement`, {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({tags}),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(apiErrorMessage(data, "Yerleşim kaydedilemedi."));
      modal.classList.remove("open");
      window.location.reload();
    } catch (error) {
      alert(error.message);
    }
  });

  const tagForm = document.getElementById("placement-tag-form");
  const tagStatus = document.getElementById("placement-tag-status");
  tagForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (tagStatus) tagStatus.textContent = "";
    try {
      const response = await fetch("/api/admin/placement-tags", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          name: document.getElementById("placement-tag-name").value.trim(),
          description: document.getElementById("placement-tag-description").value.trim(),
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(apiErrorMessage(data, "Etiket eklenemedi."));
      window.location.reload();
    } catch (error) {
      if (tagStatus) tagStatus.textContent = error.message;
      else alert(error.message);
    }
  });

  document.querySelectorAll("#placement-tags-table tr[data-tag-name]").forEach((row) => {
    const name = encodeURIComponent(row.dataset.tagName);
    row.querySelector(".placement-tag-toggle")?.addEventListener("click", async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const response = await fetch(`/api/admin/placement-tags/${name}`, {
          method: "PATCH",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({enabled: button.dataset.enabled !== "true"}),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(apiErrorMessage(data, "Etiket güncellenemedi."));
        window.location.reload();
      } catch (error) {
        alert(error.message);
        button.disabled = false;
      }
    });
    row.querySelector(".placement-tag-delete")?.addEventListener("click", async (event) => {
      if (!confirm(`"${row.dataset.tagName}" silinsin mi?`)) return;
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const response = await fetch(`/api/admin/placement-tags/${name}`, {method: "DELETE"});
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(apiErrorMessage(data, "Etiket silinemedi."));
        window.location.reload();
      } catch (error) {
        alert(error.message);
        button.disabled = false;
      }
    });
  });
}

function initMlflowSettings() {
  const form = document.getElementById("mlflow-settings-form");
  if (!form) return;
  const testButton = document.getElementById("btn-test-mlflow");
  const saveButton = document.getElementById("btn-save-mlflow");
  const status = document.getElementById("mlflow-form-status");
  const badge = document.getElementById("mlflow-status-badge");

  function payload() {
    const data = new FormData(form);
    return {
      enabled: form.elements.enabled.checked,
      auth_type: String(data.get("auth_type") || "none"),
      username: String(data.get("username") || "").trim(),
      secret: String(data.get("secret") || "") || null,
    };
  }

  async function send(url, method) {
    const response = await fetch(url, {
      method,
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload()),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || `MLflow işlemi başarısız (${response.status})`);
    return data;
  }

  testButton.addEventListener("click", async () => {
    testButton.disabled = saveButton.disabled = true;
    status.textContent = "MLflow bağlantısı test ediliyor...";
    status.className = "quota-form-status";
    try {
      const result = await send("/api/mlflow/settings/test", "POST");
      const version = result.server_version ? ` · MLflow ${result.server_version}` : "";
      const registry = result.registry_available ? "Registry hazır" : "Registry kullanılamıyor";
      status.textContent = `${result.message} Tracking hazır · ${registry}${version} · ${result.response_time_ms} ms`;
      status.className = "quota-form-status quota-status-success";
    } catch (error) {
      status.textContent = error.message;
      status.className = "quota-form-status quota-status-error";
    } finally {
      testButton.disabled = saveButton.disabled = false;
    }
  });

  form.addEventListener("submit", async event => {
    event.preventDefault();
    testButton.disabled = saveButton.disabled = true;
    status.textContent = "Kaydediliyor...";
    try {
      const result = await send("/api/mlflow/settings", "PUT");
      form.elements.secret.value = "";
      form.elements.secret.placeholder = "Kayıtlı değeri korumak için boş bırakın";
      badge.className = `badge ${result.enabled ? "badge-running" : "badge-stopped"}`;
      badge.textContent = result.enabled ? "Bağlı" : "Devre Dışı";
      status.textContent = "Kişisel MLflow bağlantınız kaydedildi.";
      status.className = "quota-form-status quota-status-success";
      window.setTimeout(() => window.location.reload(), 500);
    } catch (error) {
      status.textContent = error.message;
      status.className = "quota-form-status quota-status-error";
    } finally {
      testButton.disabled = saveButton.disabled = false;
    }
  });
}

function initAdminMlflowSettings() {
  const form = document.getElementById("admin-mlflow-settings-form");
  if (!form) return;
  const button = document.getElementById("btn-save-admin-mlflow");
  const status = document.getElementById("admin-mlflow-form-status");
  const badge = document.getElementById("admin-mlflow-status-badge");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    button.disabled = true;
    status.textContent = "Kaydediliyor...";
    status.className = "quota-form-status";
    try {
      const response = await fetch("/api/admin/mlflow-server-settings", {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          enabled: form.elements.enabled.checked,
          base_url: String(data.get("base_url") || "").trim(),
          validate_tls: form.elements.validate_tls.checked,
          ca_cert_file: String(data.get("ca_cert_file") || "").trim(),
          timeout_seconds: Number(data.get("timeout_seconds") || 10),
        }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || `Ayarlar kaydedilemedi (${response.status})`);
      badge.className = `badge ${result.enabled ? "badge-running" : "badge-stopped"}`;
      badge.textContent = result.enabled ? "Etkin" : "Devre Dışı";
      status.textContent = "MLflow sunucu politikası kaydedildi.";
      status.className = "quota-form-status quota-status-success";
    } catch (error) {
      status.textContent = error.message;
      status.className = "quota-form-status quota-status-error";
    } finally {
      button.disabled = false;
    }
  });
}

function initAdminGenAiSettings() {
  const form = document.getElementById("genai-settings-form");
  if (!form) return;
  const saveButton = document.getElementById("btn-save-genai");
  const testButton = document.getElementById("btn-test-genai");
  const status = document.getElementById("genai-form-status");
  const badge = document.getElementById("genai-status-badge");
  const showStatus = (message, kind = "") => {
    status.textContent = message;
    status.className = `quota-form-status${kind ? ` quota-status-${kind}` : ""}`;
  };
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    const adminKey = String(data.get("admin_key") || "").trim();
    const budget = String(data.get("max_budget") || "").trim();
    const payload = {
      enabled: form.elements.enabled.checked,
      auto_provision_directory_users: form.elements.auto_provision_directory_users.checked,
      base_url: String(data.get("base_url") || "").trim(),
      admin_key: form.elements.clear_admin_key.checked ? "" : (adminKey || null),
      validate_tls: form.elements.validate_tls.checked,
      ca_cert_file: String(data.get("ca_cert_file") || "").trim(),
      timeout_seconds: Number(data.get("timeout_seconds") || 15),
      user_role: String(data.get("user_role") || ""),
      models: String(data.get("models") || "").split(/\r?\n/).map((value) => value.trim()).filter(Boolean),
      max_budget: budget === "" ? null : Number(budget),
      budget_duration: String(data.get("budget_duration") || "").trim(),
      key_duration: String(data.get("key_duration") || "").trim(),
      default_team: String(data.get("default_team") || "").trim(),
      team_priority: String(data.get("team_priority") || "").split(/\r?\n/).map((value) => value.trim()).filter(Boolean),
    };
    saveButton.disabled = true;
    showStatus("Kaydediliyor...");
    try {
      const response = await fetch("/api/admin/genai-settings", {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = Array.isArray(result.detail)
          ? result.detail.map((item) => String(item.msg || "").replace(/^Value error, /, "")).join(" ")
          : result.detail;
        throw new Error(detail || `Ayarlar kaydedilemedi (${response.status})`);
      }
      badge.className = `badge ${result.enabled ? "badge-running" : "badge-stopped"}`;
      badge.textContent = result.enabled ? "Etkin" : "Devre Dışı";
      form.elements.admin_key.value = "";
      form.elements.clear_admin_key.checked = false;
      form.elements.admin_key.placeholder = result.has_admin_key
        ? "Kayıtlı anahtarı korumak için boş bırakın"
        : "aifactory kullanıcısının anahtarı";
      showStatus("GenAI ayarları kaydedildi.", "success");
    } catch (error) {
      showStatus(error.message, "error");
    } finally {
      saveButton.disabled = false;
    }
  });
  testButton.addEventListener("click", async () => {
    testButton.disabled = true;
    showStatus("Kaydedilmiş ayarlar test ediliyor...");
    try {
      const response = await fetch("/api/admin/genai-settings/test", {method: "POST"});
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || `Test başarısız (${response.status})`);
      const owner = result.admin_user_id ? ` Anahtar sahibi: ${result.admin_user_id} (${result.admin_role || "rol bilinmiyor"}).` : "";
      showStatus(`${result.message}${owner}${result.ok ? ` ${result.latency_ms} ms.` : ""}`, result.ok ? "success" : "error");
    } catch (error) {
      showStatus(error.message, "error");
    } finally {
      testButton.disabled = false;
    }
  });
}

// 6. Admin offline-download publisher
function initSessionSettings() {
  const form = document.getElementById("session-settings-form");
  if (!form) return;
  const button = form.querySelector('button[type="submit"]');
  const status = document.getElementById("session-settings-status");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    button.disabled = true;
    status.textContent = "Oturum süresi kaydediliyor...";
    status.className = "quota-form-status";
    try {
      const response = await fetch("/api/admin/session-settings", {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({timeout_minutes: Number(form.elements.timeout_minutes.value)}),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(typeof data.detail === "string" ? data.detail : `Oturum süresi kaydedilemedi (${response.status}).`);
      }
      form.elements.timeout_minutes.value = data.timeout_minutes;
      status.textContent = "Oturum süresi kaydedildi. Yeni girişlerde geçerli olacak.";
      status.className = "quota-form-status quota-status-success";
    } catch (error) {
      status.textContent = error.message;
      status.className = "quota-form-status quota-status-error";
    } finally {
      button.disabled = false;
    }
  });
}

function initOnboardingSettings() {
  const form = document.getElementById("onboarding-settings-form");
  if (!form) return;
  const button = form.querySelector('button[type="submit"]');
  const status = document.getElementById("onboarding-settings-status");
  const badge = document.getElementById("onboarding-status-badge");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    button.disabled = true;
    status.textContent = "Tur ayarları kaydediliyor...";
    status.className = "quota-form-status";
    try {
      const response = await fetch("/api/admin/onboarding-settings", {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          enabled: form.elements.enabled.checked,
          current_version: Number(form.elements.current_version.value),
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(typeof data.detail === "string" ? data.detail : `Tur ayarları kaydedilemedi (${response.status}).`);
      }
      form.elements.enabled.checked = data.enabled;
      form.elements.current_version.value = data.current_version;
      badge.className = `badge ${data.enabled ? "badge-running" : "badge-stopped"}`;
      badge.textContent = data.enabled ? "Etkin" : "Devre Dışı";
      status.textContent = data.enabled
        ? "Canlı tur etkinleştirildi. Yeni kullanıcılar giriş yaptığında tur teklifi görecek."
        : "Canlı tur devre dışı bırakıldı; kullanıcı ilerlemeleri korundu.";
      status.className = "quota-form-status quota-status-success";
    } catch (error) {
      status.textContent = error.message;
      status.className = "quota-form-status quota-status-error";
    } finally {
      button.disabled = false;
    }
  });
}

function initDownloadSettings() {
  const form = document.getElementById("download-settings-form");
  if (!form) return;
  const saveButton = document.getElementById("btn-save-download-settings");
  const status = document.getElementById("download-settings-status");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const publicBaseUrl = String(form.elements.public_base_url.value || "")
      .trim()
      .replace(/\/+$/, "");
    const workerFallbackIpv4 = String(form.elements.worker_fallback_ipv4.value || "").trim();
    saveButton.disabled = true;
    status.textContent = "Controller URL kaydediliyor...";
    status.className = "quota-form-status";
    try {
      const response = await fetch("/api/admin/download-settings", {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          public_base_url: publicBaseUrl,
          worker_fallback_ipv4: workerFallbackIpv4,
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.detail || `Controller URL kaydedilemedi (${response.status})`);
      }
      form.elements.public_base_url.value = data.public_base_url;
      form.elements.worker_fallback_ipv4.value = data.worker_fallback_ipv4 || "";
      status.textContent = "Controller FQDN ve worker rota fallback ayarları kaydedildi.";
      status.className = "quota-form-status quota-status-success";
    } catch (error) {
      status.textContent = error.message;
      status.className = "quota-form-status quota-status-error";
    } finally {
      saveButton.disabled = false;
    }
  });
}

function initHttpsSettings() {
  const form = document.getElementById("https-settings-form");
  if (!form) return;
  const button = document.getElementById("btn-apply-https-settings");
  const status = document.getElementById("https-settings-status");
  const badge = document.getElementById("https-status-badge");
  const summary = document.getElementById("https-certificate-summary");
  const agentCaSummary = document.getElementById("https-agent-ca-summary");
  const masterForm = document.getElementById("download-settings-form");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const enabling = form.elements.https_enabled.checked;
    const hasCertificate = Boolean(form.elements.certificate.files[0]);
    const hasPrivateKey = Boolean(form.elements.private_key.files[0]);
    if (hasCertificate !== hasPrivateKey) {
      status.textContent = "Sertifika ve private key dosyalarını birlikte seçin.";
      status.className = "quota-form-status quota-status-error";
      return;
    }
    if (enabling && !hasCertificate && summary.dataset.uploaded !== "true" &&
        !summary.textContent.includes("Yüklü sertifika:")) {
      status.textContent = "HTTPS'i ilk kez açmak için sertifika ve private key yükleyin.";
      status.className = "quota-form-status quota-status-error";
      return;
    }

    const payload = new FormData(form);
    payload.set("https_enabled", enabling ? "true" : "false");
    payload.set(
      "http_fallback_enabled",
      form.elements.http_fallback_enabled.checked ? "true" : "false"
    );
    button.disabled = true;
    status.textContent = "Sertifika doğrulanıyor ve Nginx yapılandırılıyor...";
    status.className = "quota-form-status";
    try {
      const response = await fetch("/api/admin/download-settings/https", {
        method: "POST",
        body: payload,
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.detail || `HTTPS ayarları uygulanamadı (${response.status})`);
      }
      form.elements.https_hostname.value = data.https_hostname;
      form.elements.https_enabled.checked = data.https_enabled;
      form.elements.http_fallback_enabled.checked = data.http_fallback_enabled;
      form.elements.certificate.value = "";
      form.elements.private_key.value = "";
      form.elements.agent_ca.value = "";
      form.elements.certificate.dispatchEvent(new Event("change"));
      form.elements.private_key.dispatchEvent(new Event("change"));
      form.elements.agent_ca.dispatchEvent(new Event("change"));
      badge.className = `badge ${data.https_enabled ? "badge-running" : "badge-stopped"}`;
      badge.textContent = data.https_enabled ? "HTTPS Etkin" : "HTTP Etkin";
      if (data.certificate_uploaded) {
        summary.dataset.uploaded = "true";
        summary.textContent = `Yüklü sertifika: ${data.certificate_subject} · Son geçerlilik: ${data.certificate_not_after} · SHA-256: ${data.certificate_sha256}`;
      }
      if (agentCaSummary && data.agent_ca_uploaded) {
        agentCaSummary.dataset.uploaded = "true";
        agentCaSummary.textContent = "Worker CA bundle kayıtlı; yeni worker bootstrap scriptlerine otomatik aktarılacak.";
      }
      if (masterForm) masterForm.elements.public_base_url.value = data.public_base_url;
      status.textContent = data.https_enabled
        ? "HTTPS etkinleştirildi. DNS ve istemci TCMB-CA güvenini ayrıca doğrulayın."
        : "HTTPS devre dışı; port 80 HTTP erişimi etkin.";
      status.className = "quota-form-status quota-status-success";
    } catch (error) {
      status.textContent = error.message;
      status.className = "quota-form-status quota-status-error";
    } finally {
      button.disabled = false;
    }
  });
}

function initDownloadUpdater() {
  const serverButton = document.getElementById("btn-update-downloads");
  const workerButton = document.getElementById("btn-update-worker-downloads");
  if (!serverButton && !workerButton) return;

  function badgeForState(state) {
    if (state === "success") return ["badge badge-running", "Hazır"];
    if (state === "failed") return ["badge badge-error", "Başarısız"];
    if (state === "running" || state === "queued") {
      return ["badge badge-creating", state === "queued" ? "Sırada" : "Oluşturuluyor"];
    }
    return ["badge badge-stopped", state === "disabled" ? "Devre Dışı" : "Beklemede"];
  }

  function createBundleController(config) {
    const button = document.getElementById(config.buttonId);
    if (!button) return null;
    const badge = document.getElementById(config.badgeId);
    const message = document.getElementById(config.messageId);
    const currentLink = document.getElementById(config.linkId);
    const logs = document.getElementById(config.logsId);
    let pollTimer = null;

    function renderStatus(data) {
      const effectiveState = data.enabled ? data.state : "disabled";
      const [badgeClass, badgeText] = badgeForState(effectiveState);
      badge.className = badgeClass;
      badge.textContent = badgeText;
      message.textContent = data.enabled
        ? (data.message || `${config.label} paketi oluşturulmaya hazır.`)
        : "İndirme güncellemeleri kapalı. Sunucuda sudo bash deploy/enable_downloads.sh komutunu çalıştırın.";

      const active = data.state === "queued" || data.state === "running";
      button.disabled = !data.enabled || active;
      button.textContent = active ? "Güncelleniyor..." : `${config.label} Paketini Güncelle`;

      if (data.current) {
        currentLink.href = data.current.download_url;
        currentLink.textContent = `${data.current.filename} (${data.current.size_display})`;
        currentLink.style.display = "inline";
      } else {
        currentLink.style.display = "none";
        currentLink.textContent = "";
      }

      const output = Array.isArray(data.logs) && data.logs.length
        ? data.logs.join("\n")
        : `Henüz ${config.label.toLowerCase()} paket logu yok.`;
      if (logs.textContent !== output) {
        logs.textContent = output;
        logs.scrollTop = logs.scrollHeight;
      }
      return active;
    }

    async function refreshStatus() {
      if (pollTimer) clearTimeout(pollTimer);
      try {
        const response = await fetch(config.statusUrl, { cache: "no-store" });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(data.detail || `Durum isteği başarısız (${response.status})`);
        }
        const active = renderStatus(data);
        pollTimer = setTimeout(refreshStatus, active ? 2000 : 10000);
      } catch (error) {
        badge.className = "badge badge-error";
        badge.textContent = "Hata";
        message.textContent = error.message;
        button.disabled = true;
        pollTimer = setTimeout(refreshStatus, 10000);
      }
    }

    button.addEventListener("click", async () => {
      const confirmed = confirm(
        `${config.label} çevrim dışı paketi oluşturulup yayımlansın mı? Bu işlem sistem RPM'lerini indirir, beş container image'ını yeniden oluşturur ve birkaç dakika sürebilir.`
      );
      if (!confirmed) return;

      button.disabled = true;
      button.textContent = "Sıraya alınıyor...";
      try {
        const response = await fetch(config.updateUrl, { method: "POST" });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(data.detail || `Güncelleme isteği başarısız (${response.status})`);
        }
        renderStatus(data);
        await refreshStatus();
      } catch (error) {
        badge.className = "badge badge-error";
        badge.textContent = "Hata";
        message.textContent = error.message;
        button.disabled = false;
        button.textContent = `${config.label} Paketini Güncelle`;
      }
    });

    refreshStatus();
    return { refreshStatus };
  }

  const controllers = [
    createBundleController({
      label: "Controller",
      buttonId: "btn-update-downloads",
      badgeId: "download-update-badge",
      messageId: "download-update-message",
      linkId: "download-current-link",
      logsId: "download-update-logs",
      statusUrl: "/api/admin/downloads/server/status",
      updateUrl: "/api/admin/downloads/server/update",
    }),
    createBundleController({
      label: "Worker",
      buttonId: "btn-update-worker-downloads",
      badgeId: "worker-download-update-badge",
      messageId: "worker-download-update-message",
      linkId: "worker-download-current-link",
      logsId: "worker-download-update-logs",
      statusUrl: "/api/admin/downloads/worker/status",
      updateUrl: "/api/admin/downloads/worker/update",
    }),
  ].filter(Boolean);

  const cleanButton = document.getElementById("btn-clean-downloads");
  if (cleanButton) {
    cleanButton.addEventListener("click", async () => {
      const confirmed = confirm(
        "Eski çevrim dışı kurulum paketleri ve geçici derleme dosyaları silinsin mi? Yalnızca en son yayımlanan paket korunacaktır."
      );
      if (!confirmed) return;

      cleanButton.disabled = true;
      cleanButton.textContent = "Temizleniyor...";
      try {
        const response = await fetch("/api/admin/downloads/clean", { method: "POST" });
        const res = await response.json();
        if (!response.ok) throw new Error(res.detail || "Temizleme başarısız.");
        alert(`Temizlik tamamlandı: ${res.cleaned_count} öğe silindi, ${res.freed_display} disk alanı kazanıldı.`);
        await Promise.all(controllers.map((controller) => controller.refreshStatus()));
      } catch (err) {
        alert(`Hata: ${err.message}`);
      } finally {
        cleanButton.disabled = false;
        cleanButton.textContent = "🧹 Eski Paketleri Temizle";
      }
    });
  }

}

// 6. Live Metrics Polling for Dashboard & Detail Views
function initLiveMetricsPolling() {
  const metricBoxes = document.querySelectorAll("[data-metrics-ws-id]");
  const detailCpu = document.getElementById("detail-val-cpu");
  const detailRam = document.getElementById("detail-val-ram");
  const detailRamPct = document.getElementById("detail-val-ram-pct");
  const detailDisk = document.getElementById("detail-val-disk");
  const detailUptime = document.getElementById("detail-val-uptime");
  const detailStatus = document.getElementById("workspace-metrics-status");
  const detailMetricsRoot = document.querySelector("[data-detail-metrics-ws-id]");
  const detailWorkspaceId = detailMetricsRoot?.dataset.detailMetricsWsId;

  if (!metricBoxes.length && !detailWorkspaceId) return;

  async function pollStats() {
    try {
      const statsMap = {};

      if (metricBoxes.length) {
        const res = await fetch("/api/workspaces/stats/summary", { cache: "no-store" });
        if (!res.ok) throw new Error(`Metrik özeti alınamadı (HTTP ${res.status})`);
        const data = await res.json();
        (data.stats || []).forEach((s) => {
          statsMap[s.workspace_id] = s;
        });
      }

      if (detailWorkspaceId) {
        const detailRes = await fetch(
          `/api/workspaces/${encodeURIComponent(detailWorkspaceId)}/stats`,
          { cache: "no-store" }
        );
        if (!detailRes.ok) {
          throw new Error(`Çalışma alanı metrikleri alınamadı (HTTP ${detailRes.status})`);
        }
        statsMap[detailWorkspaceId] = await detailRes.json();
      }

      metricBoxes.forEach((box) => {
        const wsId = box.dataset.metricsWsId;
        const st = statsMap[wsId];
        if (st) {
          const valCpu = box.querySelector(".val-cpu");
          const valRam = box.querySelector(".val-ram");
          const valDisk = box.querySelector(".val-disk");
          const valUptime = box.querySelector(".val-uptime");
          const barCpu = box.querySelector(".bar-cpu");
          const barRam = box.querySelector(".bar-ram");

          if (valCpu) valCpu.textContent = st.status === "running" ? `${st.cpu_percent}%` : "Kapalı";
          if (valRam) valRam.textContent = st.status === "running" ? st.mem_usage_display : "--";
          if (valDisk) valDisk.textContent = st.disk_usage_display || "--";
          if (valUptime) valUptime.textContent = st.uptime_display || "--";
          if (barCpu) barCpu.style.width = `${Math.min(st.cpu_percent * 2, 50)}%`;
          if (barRam) barRam.style.width = `${Math.min(st.mem_percent / 2, 50)}%`;
        }
      });

      const currentWsId = detailWorkspaceId;
      if (currentWsId && statsMap[currentWsId]) {
        const st = statsMap[currentWsId];
        const running = st.status === "running";
        if (detailCpu) detailCpu.textContent = running ? `${st.cpu_percent}%` : "—";
        if (detailRam) detailRam.textContent = running ? st.mem_usage_display : "—";
        if (detailRamPct) detailRamPct.textContent = running ? `%${st.mem_percent} ayrılan RAM` : "";
        if (detailDisk) detailDisk.textContent = st.disk_usage_display || "—";
        if (detailUptime) detailUptime.textContent = running ? st.uptime_display || "—" : "—";
        setViewStatus(detailStatus, running ? "" : "CPU ve RAM değerleri çalışma alanı çalışırken güncellenir.");
      }
    } catch (err) {
      console.warn("Metrics polling error:", err);
      if (detailMetricsRoot?.dataset.workspaceStatus === "running") {
        setViewStatus(detailStatus, "Canlı metrikler alınamadı. Yeniden deneniyor…", "warning");
        [detailCpu, detailRam, detailDisk, detailUptime].forEach(value => { value.textContent = "—"; });
        detailRamPct.textContent = "";
      }
    }
  }

  setInterval(pollStats, 4000);
  pollStats();
}

// 7. Workspace Detail Tabs
function initWorkspaceTabs() {
  const tabBtns = document.querySelectorAll(".workspace-tabs-nav button[data-tab]");
  if (!tabBtns.length) return;

  tabBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      tabBtns.forEach((b) => {
        const active = b === btn;
        b.classList.toggle("active", active);
        b.setAttribute("aria-pressed", String(active));
        document.getElementById(b.dataset.tab).hidden = !active;
      });
      if (btn.dataset.tab === "tab-files") {
        AIFactoryFileManager.init();
      }
    });
  });
}

// 8. In-Browser File Manager
const AIFactoryFileManager = {
  workspaceId: null,
  currentPath: "",
  loadVersion: 0,

  init() {
    const container = document.getElementById("tab-files");
    if (!container) return;
    this.workspaceId = container.dataset.workspaceId;

    const input = document.getElementById("fm-file-input");
    const upload = document.getElementById("fm-upload-button");
    const form = document.getElementById("fm-folder-form");
    const toggle = document.getElementById("fm-folder-toggle");
    const name = document.getElementById("fm-folder-name");
    if (!input.dataset.bound) {
      input.dataset.bound = "true";
      upload.addEventListener("click", () => input.click());
      input.addEventListener("change", async () => {
        if (!input.files.length) return;
        const body = new FormData();
        body.append("path", this.currentPath);
        for (const file of input.files) body.append("files", file);
        upload.disabled = true;
        await this.changeFiles("/upload", {method: "POST", body}, "Dosyalar yüklendi.");
        input.value = "";
        upload.disabled = false;
      });
      const showForm = open => {
        form.hidden = !open;
        toggle.setAttribute("aria-expanded", String(open));
        (open ? name : toggle).focus();
      };
      toggle.addEventListener("click", () => showForm(form.hidden));
      form.addEventListener("keydown", event => {
        if (event.key !== "Escape") return;
        event.preventDefault();
        showForm(false);
      });
      name.addEventListener("input", () => name.setCustomValidity(""));
      form.addEventListener("submit", async event => {
        event.preventDefault();
        const folder = name.value.trim();
        if (!folder) {
          name.setCustomValidity("Bir klasör adı girin.");
          name.reportValidity();
          return;
        }
        const folderPath = [this.currentPath, folder].filter(Boolean).join("/");
        event.submitter.disabled = true;
        const saved = await this.changeFiles(`/mkdir?path=${encodeURIComponent(folderPath)}`, {method: "POST"}, "Klasör oluşturuldu.");
        event.submitter.disabled = false;
        if (saved) {
          form.reset();
          showForm(false);
        }
      });
    }
    this.loadDir(this.currentPath);
  },

  async loadDir(path, focusPath = false) {
    this.currentPath = path;
    this.renderBreadcrumbs(path, focusPath);
    const version = ++this.loadVersion;
    const status = document.getElementById("fm-list-status");
    const table = document.getElementById("fm-file-table");
    table.hidden = true;
    setViewStatus(status, "Dosyalar yükleniyor…", "loading");

    try {
      const response = await fetch(`/api/workspaces/${this.workspaceId}/files?path=${encodeURIComponent(path)}`);
      const data = await response.json().catch(() => ({}));
      if (version !== this.loadVersion) return;
      if (!response.ok) throw new Error(apiErrorMessage(data, "Dosyalar listelenemedi."));
      this.currentPath = data.current_path ?? path;
      if (this.currentPath !== path) this.renderBreadcrumbs(this.currentPath, focusPath);
      const rows = (data.items || []).map(item => this.fileRow(item));
      document.getElementById("fm-file-list").replaceChildren(...rows);
      table.hidden = !rows.length;
      setViewStatus(status, rows.length ? "" : "Bu klasör boş.");
    } catch (error) {
      if (version === this.loadVersion) setViewStatus(status, error.message, "error");
    }
  },

  fileRow(item) {
    const row = document.createElement("tr");
    const name = row.insertCell();
    if (item.is_dir) {
      const folder = document.createElement("button");
      folder.type = "button";
      folder.className = "file-link";
      folder.textContent = item.name;
      folder.addEventListener("click", () => this.openFolder(item.path));
      name.append(folder);
    } else {
      name.textContent = item.name;
    }
    const size = item.size_display || "—";
    const modified = item.modified_at || "—";
    const metadata = document.createElement("small");
    metadata.className = "file-mobile-meta";
    metadata.textContent = `${size} · ${modified}`;
    name.append(metadata);
    row.insertCell().textContent = size;
    row.insertCell().textContent = modified;

    const actions = document.createElement("div");
    actions.className = "table-actions file-actions";
    if (!item.is_dir) {
      const download = document.createElement("a");
      download.className = "btn btn-secondary btn-sm";
      download.href = `/api/workspaces/${this.workspaceId}/files/download?path=${encodeURIComponent(item.path)}`;
      download.textContent = "İndir";
      download.setAttribute("aria-label", `${item.name} dosyasını indir`);
      actions.append(download);
    }
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "btn btn-danger btn-sm";
    remove.textContent = "Sil";
    remove.setAttribute("aria-label", `${item.name} öğesini sil`);
    remove.addEventListener("click", async () => {
      if (!confirm(`'${item.path}' kalıcı olarak silinsin mi?`)) return;
      remove.disabled = true;
      const deleted = await this.changeFiles(`?path=${encodeURIComponent(item.path)}`, {method: "DELETE"}, "Öğe silindi.");
      if (!deleted) remove.disabled = false;
      else if (document.activeElement === document.body) {
        document.querySelector("#fm-breadcrumbs [aria-current]")?.focus();
      }
    });
    actions.append(remove);
    row.insertCell().append(actions);
    return row;
  },

  openFolder(path) {
    setViewStatus(document.getElementById("fm-action-status"), "");
    this.loadDir(path, true);
  },

  renderBreadcrumbs(path, focusPath) {
    const nav = document.getElementById("fm-breadcrumbs");
    const parts = path.split("/").filter(Boolean);
    const labels = ["Kök klasör", ...parts];
    nav.replaceChildren(...labels.map((label, index) => {
      const current = index === labels.length - 1;
      const crumb = document.createElement(current ? "span" : "button");
      crumb.textContent = label;
      if (current) {
        crumb.setAttribute("aria-current", "location");
        crumb.tabIndex = -1;
      } else {
        crumb.type = "button";
        crumb.className = "file-link";
        crumb.addEventListener("click", () => this.openFolder(parts.slice(0, index).join("/")));
      }
      return crumb;
    }));
    if (focusPath) nav.lastElementChild.focus();
  },

  async changeFiles(suffix, options, successMessage) {
    const status = document.getElementById("fm-action-status");
    const path = this.currentPath;
    setViewStatus(status, "İşlem sürüyor…", "loading");
    try {
      const response = await fetch(`/api/workspaces/${this.workspaceId}/files${suffix}`, options);
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(apiErrorMessage(data, "Dosya işlemi tamamlanamadı."));
      }
      setViewStatus(status, successMessage, "success");
      if (path === this.currentPath) await this.loadDir(path);
      return true;
    } catch (error) {
      setViewStatus(status, error.message, "error");
      return false;
    }
  },
};

// 9. Custom Port Exposer Controller
function initPortExposer() {
  const form = document.getElementById("workspace-port-form");
  const input = document.getElementById("input-custom-port");
  if (!form || !input) return;
  form.addEventListener("submit", event => {
    event.preventDefault();
    window.open(`/proxy/${form.dataset.workspaceId}/port/${input.valueAsNumber}/`, "_blank", "noopener");
  });
}

// 10. Snapshot Modal Controller
function initSnapshotModal() {
  const modal = document.getElementById("snapshot-modal");
  const openBtn = document.getElementById("btn-open-snapshot-modal");
  const closeBtn = document.getElementById("btn-close-snapshot-modal");
  const form = document.getElementById("snapshot-form");
  if (!modal || !openBtn) return;

  openBtn.addEventListener("click", () => modal.classList.add("open"));
  if (closeBtn) closeBtn.addEventListener("click", () => modal.classList.remove("open"));

  if (form) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const wsId = form.dataset.workspaceId;
      const name = document.getElementById("input-snap-name").value.trim();
      const desc = document.getElementById("input-snap-desc").value.trim();
      const submitBtn = document.getElementById("btn-submit-snapshot");

      submitBtn.disabled = true;
      submitBtn.textContent = "Şablon oluşturuluyor (podman commit)...";

      const fd = new FormData();
      fd.append("template_name", name);
      fd.append("template_description", desc);

      try {
        const res = await fetch(`/api/workspaces/${wsId}/snapshot`, {
          method: "POST",
          body: fd,
        });
        const data = await res.json();
        if (!res.ok) {
          throw new Error(data.detail || "Şablon oluşturulamadı");
        }
        alert("🎉 " + data.message);
        modal.classList.remove("open");
      } catch (err) {
        alert("Snapshot hatası: " + err.message);
      } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = "Şablonu Kaydet (podman commit)";
      }
    });
  }
}

// 11. Admin 1-Click Platform Self-Updater & Custom Template Builder
function initAdminPlatformUpdater() {
  const commitCode = document.getElementById("admin-current-commit");
  const updateBtn = document.getElementById("btn-run-platform-update");
  const updateTerminalBox = document.getElementById("admin-update-terminal-box");
  const updateTerminal = document.getElementById("admin-update-terminal");

  const appendUpdateLine = (text) => {
    if (!updateTerminal) return;
    updateTerminal.textContent += text + "\n";
    updateTerminal.scrollTop = updateTerminal.scrollHeight;
  };

  const resetUpdateButton = () => {
    if (!updateBtn) return;
    updateBtn.disabled = false;
    updateBtn.textContent = "Platformu Güncelle";
  };

  const waitForPlatformReady = async (expectedVersion = null) => {
    appendUpdateLine("Servisin yeniden yüklenmesi ve sağlık kontrolü bekleniyor...");
    await new Promise((resolve) => setTimeout(resolve, 3500));

    const deadline = Date.now() + 90000;
    while (Date.now() < deadline) {
      try {
        const response = await fetch(`/api/admin/system/update-info?ts=${Date.now()}`, {
          cache: "no-store",
        });
        if (response.ok) {
          const info = await response.json();
          if (!expectedVersion || info.version === expectedVersion) {
            appendUpdateLine(`Servis hazır: v${info.version} (${info.commit})`);
            return true;
          }
        }
      } catch (_) {
        // A short connection failure is expected while systemd reloads workers.
      }
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }

    appendUpdateLine("❌ Servis 90 saniye içinde doğrulanamadı. Restart logunu kontrol edin.");
    return false;
  };

  const reloadWithoutCache = () => {
    const target = new URL(window.location.href);
    target.searchParams.set("_updated", Date.now().toString());
    window.location.replace(target.toString());
  };

  if (commitCode) {
    fetch(`/api/admin/system/update-info?ts=${Date.now()}`, { cache: "no-store" })
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      })
      .then((d) => {
        commitCode.textContent = `v${d.version} · ${d.branch} (${d.commit})`;
        const repository = document.getElementById("platform-update-repository");
        const ref = document.getElementById("platform-update-ref");
        if (repository && !repository.value.trim() && d.update_source) {
          repository.value = d.update_source;
        }
        if (ref && d.update_ref) ref.value = d.update_ref;
      })
      .catch(() => {
        commitCode.textContent = "Bağlantı hatası";
      });
  }

  if (updateBtn) {
    updateBtn.addEventListener("click", async () => {
      if (!confirm("Platform git sunucusundan en güncel sürüme yükseltilsin ve servis yeniden başlatılsın mı?")) return;

      updateBtn.disabled = true;
      updateBtn.textContent = "Güncelleniyor...";
      if (updateTerminalBox) updateTerminalBox.style.display = "block";
      if (updateTerminal) updateTerminal.textContent = "Güncelleme başlatılıyor...\n";

      let streamEstablished = false;
      try {
        const response = await fetch("/api/admin/system/update-stream", { method: "POST" });
        if (!response.ok) {
          const error = await response.json().catch(() => ({}));
          throw new Error(error.detail || `HTTP ${response.status}`);
        }
        if (!response.body) throw new Error("Sunucu canlı güncelleme akışı döndürmedi.");
        streamEstablished = true;
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop();

          for (const line of lines) {
            if (line.startsWith("data: ")) {
              try {
                const data = JSON.parse(line.slice(6));
                if (data.type === "log") {
                  appendUpdateLine(data.text);
                } else if (data.type === "done") {
                  appendUpdateLine("\n🎉 " + data.text);
                  if (await waitForPlatformReady(data.version)) reloadWithoutCache();
                  else resetUpdateButton();
                  return;
                } else if (data.type === "error") {
                  appendUpdateLine("\n❌ " + data.text);
                  resetUpdateButton();
                  return;
                }
              } catch (_) {}
            }
          }
        }

        appendUpdateLine("Güncelleme akışı kapandı; servis durumu kontrol ediliyor...");
        if (await waitForPlatformReady()) reloadWithoutCache();
        else resetUpdateButton();
      } catch (err) {
        if (streamEstablished) {
          appendUpdateLine("Bağlantı yeniden yükleme sırasında kesildi; servis kontrol ediliyor...");
          if (await waitForPlatformReady()) reloadWithoutCache();
          else resetUpdateButton();
        } else {
          appendUpdateLine("\n❌ Güncelleme başlatılamadı: " + err.message);
          resetUpdateButton();
        }
      }
    });
  }

  const gitUpdateForm = document.getElementById("platform-git-update-form");
  const bundleUpdateForm = document.getElementById("platform-bundle-update-form");
  const updateState = document.getElementById("admin-update-state");
  const updateStatusMessage = document.getElementById("platform-update-status-message");
  const installedVersion = document.getElementById("platform-installed-version");
  const publishedVersion = document.getElementById("platform-published-version");
  const checkMessage = document.getElementById("platform-update-check-message");
  const checkButton = document.getElementById("btn-check-platform-update");
  const applyButton = document.getElementById("btn-apply-platform-update");
  const targetVersionInput = document.getElementById("platform-update-target-version");
  let lastUpdateStatus = "";
  let checkedRelease = null;
  let updateWasActive = false;
  let refreshFailures = 0;
  let lastUpdateValue = {};
  const updateProgressBox = document.getElementById("platform-update-progress");
  const updateLiveBadge = document.getElementById("platform-log-live");

  const setStatusMessage = (text, tone = "") => {
    if (!updateStatusMessage) return;
    updateStatusMessage.textContent = text;
    updateStatusMessage.className = `platform-update-status-message${tone ? ` is-${tone}` : ""}`;
  };

  const renderQueuedUpdateStatus = (value) => {
    if (!updateState) return;
    const labels = {
      idle: "Hazır", queued: "Kuyrukta", running: "Uygulanıyor",
      succeeded: "Tamamlandı", failed: "Başarısız", unknown: "Bilinmiyor",
    };
    updateState.textContent = labels[value.state] || value.state || "Bilinmiyor";
    const stateClass = value.state === "succeeded"
      ? "badge-running"
      : value.state === "failed"
        ? "badge-error"
        : value.state === "queued" || value.state === "running"
          ? "badge-starting"
          : "badge-neutral";
    updateState.className = `badge ${stateClass}`;
    const target = value.target_version ? `v${value.target_version}` : "Güncelleme";
    const friendly = {
      idle: "Bekleyen güncelleme yok. Önce yayınlanan sürümü kontrol edebilirsiniz.",
      queued: `${target} root updater kuyruğuna alındı.`,
      running: `${target} uygulanıyor. Controller kısa süreliğine yeniden başlayabilir.`,
      succeeded: `${target} başarıyla uygulandı.`,
      failed: `${target} uygulanamadı. Teknik ayrıntı aşağıda gösteriliyor.`,
      unknown: value.error || "Güncelleme durumu doğrulanamadı.",
    }[value.state] || "Güncelleme durumu alındı.";
    if (value.state === "succeeded" && value.warning) {
      // Applied, but e.g. the worker bundle could not be published.
      setStatusMessage(`${target} uygulandı. ${value.warning}`, "warning");
    } else {
      setStatusMessage(friendly, value.state === "failed" || value.state === "unknown"
        ? "error"
        : value.state === "succeeded" ? "success" : "");
    }
    if (["queued", "running"].includes(value.state)) updateWasActive = true;
    lastUpdateValue = value;
    if (updateLiveBadge) updateLiveBadge.hidden = !UPDATE_ACTIVE_STATES.includes(value.state);
    renderUpdateProgress(updateProgressBox, value.state, value.progress, { startedAt: value.started_at });
    const serialized = JSON.stringify({ ...value, progress: undefined });
    if (serialized !== lastUpdateStatus && updateTerminal) {
      lastUpdateStatus = serialized;
      const detail = value.output || value.error || (
        value.state === "running"
          ? "Updater çıktısı bekleniyor..."
          : value.state === "queued"
            ? "Root updater'ın işi alması bekleniyor."
            : friendly
      );
      updateTerminal.textContent = detail;
      updateTerminal.scrollTop = updateTerminal.scrollHeight;
    }
    if (
      value.state === "succeeded"
      && value.target_version
      && installedVersion?.textContent !== `v${value.target_version}`
    ) {
      window.setTimeout(reloadWithoutCache, 1200);
    }
  };

  const refreshQueuedUpdateStatus = async () => {
    try {
      const response = await fetch("/api/admin/system/release-upload/status", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      refreshFailures = 0;
      renderQueuedUpdateStatus(await response.json());
    } catch (error) {
      refreshFailures += 1;
      if (updateWasActive) {
        renderUpdateProgress(updateProgressBox, "running", lastUpdateValue.progress, {
          restarting: true,
          startedAt: lastUpdateValue.started_at,
        });
        updateState.textContent = "Yeniden bağlanıyor";
        updateState.className = "badge badge-starting";
        setStatusMessage(
          "Controller güncelleme için yeniden başlatılıyor; bağlantı otomatik olarak tekrar denenecek."
        );
        return;
      }
      if (refreshFailures >= 2) {
        renderQueuedUpdateStatus({
          state: "unknown",
          error: "Güncelleme durum servisine ulaşılamadı. Sayfayı yenileyin veya controller servis durumunu kontrol edin.",
        });
      }
    }
  };

  const invalidateReleaseCheck = () => {
    checkedRelease = null;
    if (targetVersionInput) targetVersionInput.value = "";
    if (publishedVersion) publishedVersion.textContent = "Kontrol edilmedi";
    if (checkMessage) {
      checkMessage.textContent = "Repository veya branch/tag değişti; yeniden kontrol edin.";
      checkMessage.className = "platform-update-check-message";
    }
    if (applyButton) applyButton.disabled = true;
  };

  checkButton?.addEventListener("click", async () => {
    if (!gitUpdateForm?.reportValidity()) return;
    checkButton.disabled = true;
    checkButton.textContent = "Kontrol ediliyor...";
    if (checkMessage) {
      checkMessage.textContent = "Yayın kanalı okunuyor...";
      checkMessage.className = "platform-update-check-message";
    }
    try {
      const response = await fetch("/api/admin/system/release-check", {
        method: "POST",
        body: new FormData(gitUpdateForm),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || "Yayınlanan sürüm kontrol edilemedi.");
      checkedRelease = data;
      if (installedVersion) installedVersion.textContent = `v${data.installed_version}`;
      if (publishedVersion) publishedVersion.textContent = `v${data.published_version}`;
      if (targetVersionInput) targetVersionInput.value = data.published_version;
      if (data.published_is_older) {
        checkMessage.textContent = "Yayınlanan sürüm kurulu sürümden eski; sürüm düşürme engellendi.";
        checkMessage.className = "platform-update-check-message is-error";
        applyButton.disabled = true;
      } else if (data.update_available) {
        checkMessage.textContent = `v${data.published_version} kurulabilir. Bundle doğrulandı; yüklemeyi başlatabilirsiniz.`;
        checkMessage.className = "platform-update-check-message is-success";
        applyButton.disabled = false;
      } else {
        checkMessage.textContent = `Platform güncel: v${data.installed_version}.`;
        checkMessage.className = "platform-update-check-message is-success";
        applyButton.disabled = true;
      }
    } catch (error) {
      checkedRelease = null;
      if (publishedVersion) publishedVersion.textContent = "Kontrol başarısız";
      if (targetVersionInput) targetVersionInput.value = "";
      if (applyButton) applyButton.disabled = true;
      if (checkMessage) {
        checkMessage.textContent = error.message;
        checkMessage.className = "platform-update-check-message is-error";
      }
    } finally {
      checkButton.disabled = false;
      checkButton.textContent = "Güncellemeyi Kontrol Et";
    }
  });

  ["platform-update-repository", "platform-update-ref"].forEach((id) => {
    document.getElementById(id)?.addEventListener("input", invalidateReleaseCheck);
  });

  const submitQueuedUpdate = async (form, endpoint, signedConfirmation) => {
    const allowUnsigned = form.elements.namedItem("allow_unsigned")?.checked === true;
    const confirmation = allowUnsigned
      ? "DİKKAT: Release imzası doğrulanmayacak. Kaynağa güvendiğinizi onaylıyor ve güncellemeyi başlatmak istiyor musunuz?"
      : signedConfirmation;
    if (!confirm(confirmation)) return;
    const button = form.querySelector('button[type="submit"]');
    const defaultButtonText = button.textContent;
    button.disabled = true;
    button.textContent = "Kuyruğa alınıyor...";
    let queued = false;
    try {
      const response = await fetch(endpoint, { method: "POST", body: new FormData(form) });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
      queued = true;
      updateWasActive = true;
      renderQueuedUpdateStatus(data);
    } catch (error) {
      renderQueuedUpdateStatus({ state: "failed", error: error.message });
    } finally {
      button.disabled = queued || (button === applyButton && !checkedRelease);
      button.textContent = defaultButtonText;
    }
  };

  gitUpdateForm?.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!checkedRelease?.update_available) {
      if (checkMessage) {
        checkMessage.textContent = "Güncellemeyi yüklemeden önce yayınlanan sürümü kontrol edin.";
        checkMessage.className = "platform-update-check-message is-error";
      }
      return;
    }
    submitQueuedUpdate(
      gitUpdateForm,
      "/api/admin/system/release-source",
      "Git kanalındaki imzalı platform release'i doğrulanıp uygulansın mı?",
    );
  });
  bundleUpdateForm?.addEventListener("submit", (event) => {
    event.preventDefault();
    submitQueuedUpdate(
      bundleUpdateForm,
      "/api/admin/system/release-upload",
      "Seçilen platform bundle imzası doğrulanıp uygulansın mı?",
    );
  });
  if (gitUpdateForm || bundleUpdateForm) {
    // Poll every 2 s while an update runs, otherwise every 5 s.
    const pollQueuedUpdateStatus = async () => {
      await refreshQueuedUpdateStatus();
      const active = updateWasActive && !["succeeded", "failed", "idle"].includes(lastUpdateValue.state);
      window.setTimeout(pollQueuedUpdateStatus, active ? 2000 : 5000);
    };
    pollQueuedUpdateStatus();
  }

  const tbModal = document.getElementById("template-builder-modal");
  const tbOpenBtn = document.getElementById("btn-open-template-builder-modal");
  const tbCloseBtn = document.getElementById("btn-close-template-builder-modal");
  const tbForm = document.getElementById("template-builder-form");
  const tbTerminalBox = document.getElementById("tb-terminal-box");
  const tbTerminal = document.getElementById("tb-terminal");

  if (tbOpenBtn && tbModal) {
    tbOpenBtn.addEventListener("click", () => tbModal.classList.add("open"));
  }
  if (tbCloseBtn && tbModal) {
    tbCloseBtn.addEventListener("click", () => tbModal.classList.remove("open"));
  }

  if (tbForm) {
    tbForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const submitBtn = document.getElementById("btn-submit-tb");
      submitBtn.disabled = true;
      submitBtn.textContent = "Derleniyor (podman build)...";
      if (tbTerminalBox) tbTerminalBox.style.display = "block";
      if (tbTerminal) tbTerminal.textContent = "Container derlemesi başlatılıyor...\n";

      const fd = new FormData();
      fd.append("template_id", document.getElementById("tb-input-id").value.trim());
      fd.append("name", document.getElementById("tb-input-name").value.trim());
      fd.append("category", document.getElementById("tb-input-category").value.trim());
      fd.append("default_port", document.getElementById("tb-input-port").value);
      fd.append("ide_type", document.getElementById("tb-input-ide").value);
      fd.append("description", document.getElementById("tb-input-desc").value.trim());
      fd.append("containerfile", document.getElementById("tb-input-containerfile").value);

      try {
        const response = await fetch("/api/admin/templates/build-stream", {
          method: "POST",
          body: fd,
        });

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop();

          for (const line of lines) {
            if (line.startsWith("data: ")) {
              try {
                const data = JSON.parse(line.slice(6));
                if (data.type === "log") {
                  tbTerminal.textContent += data.text + "\n";
                  tbTerminal.scrollTop = tbTerminal.scrollHeight;
                } else if (data.type === "done") {
                  tbTerminal.textContent += "\n🎉 Şablon başarıyla derlendi ve kaydedildi!\n";
                  setTimeout(() => {
                    tbModal.classList.remove("open");
                    window.location.reload();
                  }, 2000);
                } else if (data.type === "error") {
                  tbTerminal.textContent += "\n❌ " + data.text + "\n";
                  submitBtn.disabled = false;
                  submitBtn.textContent = "Yeniden Dene";
                }
              } catch (_) {}
            }
          }
        }
      } catch (err) {
        tbTerminal.textContent += "\nHata: " + err.message + "\n";
        submitBtn.disabled = false;
        submitBtn.textContent = "Yeniden Dene";
      }
    });
  }
}


function initWorkspaceShares() {
  const form = document.getElementById("workspace-share-form");
  if (!form) return;
  const endpoint = `/api/workspaces/${form.dataset.workspaceId}/shares`;
  const status = document.getElementById("workspace-share-status");
  const list = document.getElementById("workspace-share-list");
  const submit = form.querySelector('button[type="submit"]');
  async function api(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(typeof data.detail === "string" ? data.detail : `İşlem başarısız (${response.status}).`);
    }
    return response.status === 204 ? null : response.json();
  }
  async function load() {
    const shares = await api(endpoint);
    list.replaceChildren();
    for (const share of shares) {
      const row = document.createElement("div");
      row.className = "record-row";
      const field = document.createElement("div");
      const label = document.createElement("label");
      label.className = "form-label";
      const expires = share.expires_at ? new Date(share.expires_at * 1000).toLocaleString() : "Süresiz";
      label.textContent = `Port ${share.port} · ${share.password_protected ? "Parolalı" : "Parolasız"} · ${expires}${share.expires_at && share.expires_at * 1000 <= Date.now() ? " (Süresi doldu)" : ""}`;
      const input = document.createElement("input");
      input.id = `share-url-${share.id}`;
      label.htmlFor = input.id;
      input.className = "form-input";
      input.readOnly = true;
      input.value = new URL(share.url, window.location.origin).href;
      input.addEventListener("click", () => input.select());
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "btn btn-secondary btn-sm";
      copy.textContent = "Kopyala";
      copy.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(input.value);
          setViewStatus(status, "Bağlantı kopyalandı.", "success");
        } catch (_) {
          input.focus(); input.select();
          setViewStatus(status, "Bağlantı seçildi; kopyalayabilirsiniz.");
        }
      });
      const revoke = document.createElement("button");
      revoke.type = "button";
      revoke.className = "btn btn-danger btn-sm";
      revoke.textContent = "İptal et";
      revoke.addEventListener("click", async () => {
        revoke.disabled = true;
        try {
          await api(`${endpoint}/${share.id}`, {method: "DELETE"});
          await load();
          setViewStatus(status, "Bağlantı iptal edildi.", "success");
        } catch (error) { setViewStatus(status, error.message, "error"); revoke.disabled = false; }
      });
      field.append(label, input);
      const actions = document.createElement("div");
      actions.className = "table-actions record-actions";
      actions.append(copy, revoke);
      row.append(field, actions);
      list.append(row);
    }
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const port = Number(document.getElementById("input-custom-port").value);
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      setViewStatus(status, "Geçerli bir port numarası girin (1–65535).", "error");
      return;
    }
    submit.disabled = true;
    try {
      await api(endpoint, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
        port, password: form.elements.password.value || null,
        expires_in_minutes: form.elements.duration.value ? Number(form.elements.duration.value) : null,
      })});
      form.elements.password.value = "";
      await load();
      setViewStatus(status, "Paylaşım bağlantısı oluşturuldu.", "success");
    } catch (error) { setViewStatus(status, error.message, "error"); }
    finally { submit.disabled = false; }
  });
  load().catch(error => setViewStatus(status, error.message, "error"));
}
