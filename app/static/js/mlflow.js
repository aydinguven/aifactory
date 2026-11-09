(() => {
  "use strict";

  const esc = value => String(value ?? "").replace(
    /[&<>'"]/g,
    character => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[character],
  );
  const formatDate = value => value ? new Date(Number(value)).toLocaleString("tr-TR") : "—";
  const statusClass = value => {
    const normalized = String(value || "").toUpperCase();
    if (normalized === "FINISHED" || normalized === "READY") return "badge-running";
    if (normalized === "FAILED" || normalized === "KILLED") return "badge-error";
    return "badge-neutral";
  };
  const fetchJson = async url => {
    const response = await fetch(url, {cache: "no-store"});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || `MLflow isteği başarısız (${response.status})`);
    return data;
  };

  function initDashboard() {
    const root = document.getElementById("mlflow-dashboard");
    if (!root) return;
    const status = document.getElementById("mlflow-overview-status");
    const content = document.getElementById("mlflow-overview-content");
    fetchJson("/api/mlflow/overview").then(data => {
      const partial = value => value ? "+" : "";
      document.getElementById("mlflow-stat-experiments").textContent = `${data.experiment_count}${partial(data.experiment_count_is_partial)}`;
      document.getElementById("mlflow-stat-models").textContent = `${data.model_count}${partial(data.model_count_is_partial)}`;
      document.getElementById("mlflow-stat-runs").textContent = data.sampled_run_count;
      document.getElementById("mlflow-stat-latency").textContent = `${data.response_time_ms} ms`;

      const statuses = document.getElementById("mlflow-status-summary");
      const statusEntries = Object.entries(data.status_counts || {}).sort((a, b) => b[1] - a[1]);
      statuses.innerHTML = statusEntries.map(([name, count]) => `<div class="mlflow-status-item"><span class="badge ${statusClass(name)}">${esc(name)}</span><strong>${Number(count)}</strong></div>`).join("") || '<p class="text-muted">Henüz run bulunamadı.</p>';

      document.getElementById("mlflow-recent-models").innerHTML = (data.recent_models || []).map(model => {
        const latest = model.latest_version || {};
        return `<div class="mlflow-compact-item"><div><a href="/models/${encodeURIComponent(model.name)}"><strong>${esc(model.name)}</strong></a><br><span class="text-muted">${esc(model.description || "Açıklama yok")}</span></div><span class="badge badge-neutral">${latest.version ? `v${esc(latest.version)}` : "—"}</span></div>`;
      }).join("") || '<p class="text-muted">Kayıtlı model bulunamadı.</p>';

      document.getElementById("mlflow-recent-runs").innerHTML = (data.recent_runs || []).map(run => `<tr>
        <td><a href="/runs/${encodeURIComponent(run.run_id)}"><strong>${esc(run.run_name || run.run_id)}</strong></a><br><code>${esc(run.run_id)}</code></td>
        <td><span class="badge ${statusClass(run.status)}">${esc(run.status || "UNKNOWN")}</span></td>
        <td>${formatDate(run.start_time)}</td>
        <td>${Object.keys(run.metrics_map || {}).length}</td>
        <td>${Object.keys(run.params_map || {}).length}</td>
        <td><a href="${esc(run.mlflow_url)}" target="_blank" rel="noopener noreferrer">MLflow ↗</a></td>
      </tr>`).join("") || '<tr><td colspan="6" class="text-muted">Henüz run bulunamadı.</td></tr>';
      status.textContent = "";
      content.hidden = false;
    }).catch(error => {
      status.textContent = error.message;
      status.className = "text-destructive";
    });
  }

  const svgNode = (name, attributes = {}) => {
    const node = document.createElementNS("http://www.w3.org/2000/svg", name);
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value));
    return node;
  };

  function renderLineChart(container, series, metricKey) {
    container.innerHTML = "";
    const cleanSeries = series.map(item => ({
      ...item,
      points: (item.points || []).map(point => ({
        step: Number(point.step || 0),
        timestamp: Number(point.timestamp || 0),
        value: Number(point.value),
      })).filter(point => Number.isFinite(point.value)),
    })).filter(item => item.points.length);
    const all = cleanSeries.flatMap(item => item.points);
    if (!all.length) {
      container.innerHTML = '<p class="text-muted">Bu metrik için geçmiş değer bulunamadı.</p>';
      return;
    }
    const useStep = new Set(all.map(point => point.step)).size > 1;
    const xValue = point => useStep ? point.step : (point.timestamp || 0);
    let minX = Math.min(...all.map(xValue));
    let maxX = Math.max(...all.map(xValue));
    let minY = Math.min(...all.map(point => point.value));
    let maxY = Math.max(...all.map(point => point.value));
    if (minX === maxX) { minX -= 1; maxX += 1; }
    if (minY === maxY) { minY -= 1; maxY += 1; }

    const width = 760, height = 300, left = 58, right = 18, top = 22, bottom = 42;
    const plotWidth = width - left - right, plotHeight = height - top - bottom;
    const x = value => left + ((value - minX) / (maxX - minX)) * plotWidth;
    const y = value => top + (1 - ((value - minY) / (maxY - minY))) * plotHeight;
    const svg = svgNode("svg", {viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": `${metricKey} metrik grafiği`});
    for (let index = 0; index <= 4; index += 1) {
      const gridY = top + (plotHeight * index / 4);
      svg.appendChild(svgNode("line", {x1:left, y1:gridY, x2:width-right, y2:gridY, stroke:"#dbe2ea", "stroke-width":"1"}));
      const label = svgNode("text", {x:left-8, y:gridY+4, "text-anchor":"end", fill:"#657186", "font-size":"11"});
      label.textContent = (maxY - ((maxY - minY) * index / 4)).toPrecision(4);
      svg.appendChild(label);
    }
    const colors = ["#d50032", "#2563eb", "#059669", "#7c3aed", "#d97706", "#0891b2", "#db2777", "#4f46e5", "#65a30d", "#9333ea"];
    cleanSeries.forEach((item, index) => {
      const coordinates = item.points.map(point => `${x(xValue(point)).toFixed(2)},${y(point.value).toFixed(2)}`).join(" ");
      svg.appendChild(svgNode("polyline", {points:coordinates, fill:"none", stroke:colors[index % colors.length], "stroke-width":"2.5", "stroke-linecap":"round", "stroke-linejoin":"round"}));
    });
    const axisLabel = svgNode("text", {x:left + plotWidth / 2, y:height-10, "text-anchor":"middle", fill:"#657186", "font-size":"12"});
    axisLabel.textContent = useStep ? "Adım" : "Zaman / kayıt sırası";
    svg.appendChild(axisLabel);
    container.appendChild(svg);
    const legend = document.createElement("div");
    legend.className = "mlflow-chart-legend";
    cleanSeries.forEach((item, index) => {
      const entry = document.createElement("span");
      const swatch = document.createElement("i");
      swatch.className = "mlflow-chart-swatch";
      swatch.style.background = colors[index % colors.length];
      entry.appendChild(swatch);
      entry.append(document.createTextNode(item.name));
      legend.appendChild(entry);
    });
    container.appendChild(legend);
  }

  function initExperimentDetail() {
    const root = document.getElementById("experiment-root");
    if (!root) return;
    const id = root.dataset.experimentId;
    const status = document.getElementById("run-status");
    const table = document.getElementById("run-table");
    const tbody = table.querySelector("tbody");
    const more = document.getElementById("run-more");
    const compare = document.getElementById("compare-runs");
    const filter = document.getElementById("run-filter");
    const chips = document.getElementById("mlflow-filter-chips");
    const scope = document.getElementById("filter-scope");
    const key = document.getElementById("filter-key");
    const operator = document.getElementById("filter-operator");
    const value = document.getElementById("filter-value");
    const builderStatus = document.getElementById("filter-builder-status");
    const savedSelect = document.getElementById("saved-filter-select");
    let token = "", total = 0, conditions = [];
    const selected = () => [...document.querySelectorAll("[data-run-select]:checked")].map(item => item.value);
    const syncCompare = () => {
      const count = selected().length;
      compare.disabled = count < 2 || count > 10;
      compare.textContent = count ? `${count} run'ı karşılaştır` : "Seçilen run'ları karşılaştır";
    };
    const readSaved = () => {
      try {
        // Filters saved before 3.49.1 use the devcloud key; move them once.
        const legacy = localStorage.getItem("devcloud.mlflow.savedFilters.v1");
        if (legacy !== null && localStorage.getItem("aifactory.mlflow.savedFilters.v1") === null) {
          localStorage.setItem("aifactory.mlflow.savedFilters.v1", legacy);
        }
        localStorage.removeItem("devcloud.mlflow.savedFilters.v1");
        return JSON.parse(localStorage.getItem("aifactory.mlflow.savedFilters.v1") || "{}");
      }
      catch (_) { return {}; }
    };
    const writeSaved = data => {
      try { localStorage.setItem("aifactory.mlflow.savedFilters.v1", JSON.stringify(data)); }
      catch (_) { builderStatus.textContent = "Tarayıcı filtreleri kaydedemedi."; }
    };
    const renderSaved = () => {
      const saved = readSaved();
      savedSelect.replaceChildren(new Option("Kayıtlı görünüm seç", ""));
      Object.keys(saved).sort((a, b) => a.localeCompare(b, "tr")).forEach(name => savedSelect.add(new Option(name, name)));
    };
    const renderChips = () => {
      chips.innerHTML = conditions.map((condition, index) => `<span class="mlflow-filter-chip"><code>${esc(condition.label)}</code><button type="button" data-remove-filter="${index}" aria-label="Filtreyi kaldır">×</button></span>`).join("");
      filter.value = conditions.map(condition => condition.expression).join(" AND ");
    };
    const syncBuilder = () => {
      const isStatus = scope.value === "attributes.status";
      const isMetric = scope.value === "metrics";
      key.disabled = isStatus;
      key.placeholder = isStatus ? "Gerekmez" : (isMetric ? "accuracy" : "owner");
      operator.innerHTML = (isMetric ? [">", ">=", "<", "<=", "=", "!="] : ["=", "!="]).map(item => `<option value="${esc(item)}">${esc(item)}</option>`).join("");
      value.placeholder = isStatus ? "FINISHED" : (isMetric ? "0.90" : "değer");
    };
    const addCondition = () => {
      const kind = scope.value;
      const fieldKey = key.value.trim();
      const rawValue = value.value.trim();
      builderStatus.textContent = "";
      if (!rawValue) throw new Error("Filtre değeri boş olamaz.");
      let expression;
      let label;
      if (kind === "attributes.status") {
        if (!/^[A-Za-z_]+$/.test(rawValue)) throw new Error("Geçersiz run durumu.");
        expression = `attributes.status ${operator.value} '${rawValue.toUpperCase()}'`;
        label = `Durum ${operator.value} ${rawValue.toUpperCase()}`;
      } else {
        if (!fieldKey || fieldKey.includes("`") || fieldKey.includes("'")) throw new Error("Geçerli bir alan adı girin.");
        if (kind === "metrics") {
          const number = Number(rawValue);
          if (!Number.isFinite(number)) throw new Error("Metrik filtresi sayısal olmalıdır.");
          expression = `metrics.\`${fieldKey}\` ${operator.value} ${number}`;
        } else {
          if (rawValue.includes("'")) throw new Error("Filtre değeri tek tırnak içeremez.");
          expression = `${kind}.\`${fieldKey}\` ${operator.value} '${rawValue}'`;
        }
        label = `${kind}.${fieldKey} ${operator.value} ${rawValue}`;
      }
      conditions.push({expression, label});
      value.value = "";
      renderChips();
    };

    async function load(append = false) {
      const query = new URLSearchParams({experiment_id:id});
      if (filter.value.trim()) query.set("filter_string", filter.value.trim());
      if (append && token) query.set("page_token", token);
      const data = await fetchJson(`/api/mlflow/runs?${query}`);
      if (!append) { tbody.innerHTML = ""; total = 0; }
      tbody.insertAdjacentHTML("beforeend", (data.runs || []).map(run => `<tr>
        <td><input type="checkbox" data-run-select value="${esc(run.run_id)}" aria-label="${esc(run.run_name)} seç"></td>
        <td><a href="/runs/${encodeURIComponent(run.run_id)}"><strong>${esc(run.run_name)}</strong></a><br><code>${esc(run.run_id)}</code></td>
        <td><span class="badge ${statusClass(run.status)}">${esc(run.status || "—")}</span></td><td>${formatDate(run.start_time)}</td>
        <td>${Object.keys(run.metrics_map || {}).length}</td><td>${Object.keys(run.params_map || {}).length}</td>
        <td><a href="${esc(run.mlflow_url)}" target="_blank" rel="noopener noreferrer">MLflow ↗</a></td>
      </tr>`).join(""));
      total += (data.runs || []).length;
      token = data.next_page_token || "";
      document.getElementById("run-count").textContent = `${total} run`;
      table.hidden = !total;
      status.textContent = total ? "" : "Run bulunamadı.";
      more.hidden = !token;
      syncCompare();
    }
    const fail = error => { status.textContent = error.message; status.className = "text-destructive"; };
    root.addEventListener("change", event => { if (event.target.matches("[data-run-select]")) syncCompare(); });
    chips.addEventListener("click", event => {
      const button = event.target.closest("[data-remove-filter]");
      if (!button) return;
      conditions.splice(Number(button.dataset.removeFilter), 1);
      renderChips();
    });
    scope.addEventListener("change", syncBuilder);
    document.getElementById("add-filter-condition").addEventListener("click", () => {
      try { addCondition(); } catch (error) { builderStatus.textContent = error.message; }
    });
    document.getElementById("run-filter-form").addEventListener("submit", event => { event.preventDefault(); load().catch(fail); });
    document.getElementById("save-filter").addEventListener("click", () => {
      const name = document.getElementById("saved-filter-name").value.trim();
      if (!name || !filter.value.trim()) { builderStatus.textContent = "Görünüm adı ve filtre gereklidir."; return; }
      const saved = readSaved(); saved[name] = filter.value.trim(); writeSaved(saved); renderSaved(); savedSelect.value = name; builderStatus.textContent = "Filtre görünümü kaydedildi.";
    });
    document.getElementById("load-filter").addEventListener("click", () => {
      const saved = readSaved(); if (!savedSelect.value || !saved[savedSelect.value]) return;
      conditions = []; chips.innerHTML = ""; filter.value = saved[savedSelect.value]; load().catch(fail);
    });
    document.getElementById("delete-filter").addEventListener("click", () => {
      if (!savedSelect.value) return; const saved = readSaved(); delete saved[savedSelect.value]; writeSaved(saved); renderSaved(); builderStatus.textContent = "Kayıtlı görünüm silindi.";
    });
    more.addEventListener("click", () => load(true).catch(fail));
    compare.addEventListener("click", () => { const query = new URLSearchParams(); selected().forEach(runId => query.append("run_ids", runId)); location.href = `/runs/compare?${query}`; });
    syncBuilder(); renderSaved();
    Promise.all([
      fetchJson(`/api/mlflow/experiments/${encodeURIComponent(id)}`).then(data => { document.getElementById("experiment-name").textContent = data.name || id; document.getElementById("experiment-open").href = data.mlflow_url; }),
      load(),
    ]).catch(fail);
  }

  function renderRunValues(values, { kind = "parameter", metadata = new Map() } = {}) {
    const entries = Object.entries(values || {}).sort(([left], [right]) => left.localeCompare(right, "tr-TR"));
    if (!entries.length) return '<div class="mlflow-value-empty"><dt class="sr-only">Durum</dt><dd class="text-muted">Veri yok</dd></div>';
    return entries.map(([key, value]) => {
      const rawValue = String(value ?? "");
      const numericValue = Number(value);
      const displayValue = kind === "metric" && Number.isFinite(numericValue)
        ? numericValue.toLocaleString("tr-TR", { maximumSignificantDigits: 9 })
        : rawValue || "Boş değer";
      const metric = metadata.get(key) || {};
      const details = [];
      const metricStep = Number(metric.step);
      if (kind === "metric" && metric.step !== undefined && metric.step !== null && Number.isFinite(metricStep)) details.push(`Adım ${metricStep.toLocaleString("tr-TR")}`);
      if (kind === "metric" && metric.timestamp) details.push(formatDate(metric.timestamp));
      return `<div class="mlflow-value-item mlflow-value-item--${kind}">
        <dt title="${esc(key)}">${esc(key)}</dt>
        <dd><code title="${esc(rawValue)}">${esc(displayValue)}</code>${details.length ? `<small>${esc(details.join(" · "))}</small>` : ""}</dd>
      </div>`;
    }).join("");
  }

  function initRunDetail() {
    const root = document.getElementById("run-root");
    if (!root) return;
    const runId = root.dataset.runId;
    const status = document.getElementById("run-status");
    const content = document.getElementById("run-content");
    const metricSelect = document.getElementById("run-metric-select");
    const chart = document.getElementById("run-metric-chart");
    const artifactList = document.getElementById("run-artifact-list");
    const artifactStatus = document.getElementById("run-artifact-status");
    const artifactPath = document.getElementById("run-artifact-path");
    const artifactBack = document.getElementById("run-artifact-back");
    const preview = document.getElementById("run-artifact-preview");
    let currentPath = "";
    const canPreview = path => /\.(cfg|conf|csv|gif|ini|jpe?g|json|log|md|png|py|toml|tsv|txt|webp|xml|ya?ml)$/i.test(path);

    async function loadMetric() {
      if (!metricSelect.value) { chart.innerHTML = '<p class="text-muted">Grafik için bir metrik seçin.</p>'; return; }
      chart.innerHTML = '<p class="text-muted">Metrik geçmişi yükleniyor...</p>';
      const data = await fetchJson(`/api/mlflow/runs/${encodeURIComponent(runId)}/metrics/${encodeURIComponent(metricSelect.value)}/history`);
      renderLineChart(chart, [{name:document.getElementById("run-name").textContent, points:data.points}], metricSelect.value);
    }
    async function loadArtifacts(path = "") {
      currentPath = path;
      artifactPath.textContent = path || "Kök dizin";
      artifactBack.hidden = !path;
      artifactStatus.textContent = "Artifact'lar yükleniyor...";
      preview.hidden = true;
      const query = new URLSearchParams(); if (path) query.set("path", path);
      const data = await fetchJson(`/api/mlflow/runs/${encodeURIComponent(runId)}/artifacts?${query}`);
      artifactList.innerHTML = (data.files || []).map(item => {
        const pathValue = String(item.path || "");
        const action = item.is_dir
          ? `<button class="btn btn-secondary btn-sm" type="button" data-artifact-dir="${esc(pathValue)}">Klasörü aç</button>`
          : canPreview(pathValue)
            ? `<button class="btn btn-secondary btn-sm" type="button" data-artifact-preview="${esc(pathValue)}">Önizle</button>`
            : '<span class="badge badge-neutral">Önizleme yok</span>';
        return `<div class="mlflow-artifact-item"><div><strong>${item.is_dir ? "📁" : "📄"}</strong> <code>${esc(pathValue)}</code><br><span class="text-muted">${item.is_dir ? "Klasör" : `${Number(item.file_size || 0).toLocaleString("tr-TR")} bayt`}</span></div>${action}</div>`;
      }).join("") || '<p class="text-muted">Bu dizinde artifact bulunamadı.</p>';
      artifactStatus.textContent = "";
    }
    async function showPreview(path) {
      preview.hidden = false;
      preview.textContent = "Artifact önizlemesi yükleniyor...";
      const data = await fetchJson(`/api/mlflow/runs/${encodeURIComponent(runId)}/artifacts/preview?path=${encodeURIComponent(path)}`);
      preview.replaceChildren();
      if (data.kind === "image") {
        const image = document.createElement("img"); image.alt = path; image.src = `data:${data.content_type};base64,${data.content_base64}`; preview.appendChild(image);
      } else {
        const pre = document.createElement("pre"); pre.textContent = data.content || ""; preview.appendChild(pre);
      }
    }
    artifactList.addEventListener("click", event => {
      const directory = event.target.closest("[data-artifact-dir]");
      const file = event.target.closest("[data-artifact-preview]");
      if (directory) loadArtifacts(directory.dataset.artifactDir).catch(error => { artifactStatus.textContent = error.message; artifactStatus.className = "text-destructive"; });
      if (file) showPreview(file.dataset.artifactPreview).catch(error => { preview.hidden = false; preview.textContent = error.message; });
    });
    artifactBack.addEventListener("click", () => { const parts = currentPath.split("/").filter(Boolean); parts.pop(); loadArtifacts(parts.join("/")).catch(error => { artifactStatus.textContent = error.message; }); });
    metricSelect.addEventListener("change", () => loadMetric().catch(error => { chart.textContent = error.message; }));

    fetchJson(`/api/mlflow/runs/${encodeURIComponent(runId)}`).then(data => {
      document.getElementById("run-name").textContent = data.run_name || data.run_id;
      document.getElementById("run-open").href = data.mlflow_url;
      document.getElementById("run-state").textContent = data.status || "UNKNOWN";
      document.getElementById("run-start").textContent = formatDate(data.start_time);
      document.getElementById("run-end").textContent = formatDate(data.end_time);
      const params = data.params_map || {};
      const metrics = data.metrics_map || {};
      const metricMetadata = new Map((data.data?.metrics || []).map(metric => [String(metric.key || ""), metric]));
      const modelVersions = data.registered_model_versions || [];
      document.getElementById("run-params").innerHTML = renderRunValues(params);
      document.getElementById("run-metrics").innerHTML = renderRunValues(metrics, { kind: "metric", metadata: metricMetadata });
      document.getElementById("run-param-count").textContent = `${Object.keys(params).length.toLocaleString("tr-TR")} parametre`;
      document.getElementById("run-metric-count").textContent = `${Object.keys(metrics).length.toLocaleString("tr-TR")} metrik`;
      document.getElementById("run-lineage-count").textContent = `${modelVersions.length.toLocaleString("tr-TR")} model sürümü`;
      document.getElementById("run-lineage").innerHTML = modelVersions.map(version => `<article class="mlflow-lineage-item">
        <div class="mlflow-lineage-node mlflow-lineage-node--run"><span>Kaynak run</span><strong>${esc(data.run_name || data.run_id)}</strong><code>${esc(data.run_id)}</code></div>
        <span class="mlflow-lineage-arrow" aria-hidden="true">→</span>
        <div class="mlflow-lineage-node mlflow-lineage-node--model"><span>Kayıtlı model</span><div><a class="admin-user-name" href="/models/${encodeURIComponent(version.name)}">${esc(version.name)}</a> <span class="badge badge-neutral">v${esc(version.version)}</span></div></div>
        <div class="mlflow-lineage-actions"><button type="button" class="btn btn-primary btn-sm" data-deploy-model data-model-name="${esc(version.name)}" data-model-version="${esc(version.version)}" data-run-id="${esc(version.run_id || data.run_id)}">Dağıt</button><a class="btn btn-secondary btn-sm" href="${esc(version.mlflow_url)}" target="_blank" rel="noopener noreferrer">MLflow'da aç ↗</a></div>
      </article>`).join("") || '<div class="mlflow-lineage-empty"><strong>Henüz kayıtlı model yok</strong><p class="text-muted">Bu run ile ilişkili bir model sürümü kaydedildiğinde burada görünecek.</p></div>';
      if ((data.warnings || []).length) {
        status.textContent = data.warnings.join(" ");
        status.className = "text-muted";
      } else {
        status.textContent = "";
      }
      const metricKeys = Object.keys(data.metrics_map || {}).sort((a, b) => a.localeCompare(b));
      metricSelect.innerHTML = metricKeys.map(key => `<option value="${esc(key)}">${esc(key)}</option>`).join("");
      content.hidden = false;
      if (metricKeys.length) loadMetric().catch(error => { chart.textContent = error.message; });
      loadArtifacts().catch(error => { artifactStatus.textContent = error.message; artifactStatus.className = "text-destructive"; });
    }).catch(error => { status.textContent = error.message; status.className = "text-destructive"; });
  }

  function initRunCompare() {
    const root = document.getElementById("compare-root");
    if (!root) return;
    const status = document.getElementById("compare-status");
    const content = document.getElementById("compare-content");
    const chart = document.getElementById("compare-metric-chart");
    const metricSelect = document.getElementById("compare-metric-select");
    const ids = new URLSearchParams(location.search).getAll("run_ids");
    if (ids.length < 2) { status.textContent = "Karşılaştırmak için en az iki run seçin."; status.className = "text-destructive"; return; }
    const query = new URLSearchParams(); ids.forEach(id => query.append("run_ids", id));
    let runs = [];
    async function loadComparisonChart() {
      const metric = metricSelect.value;
      if (!metric) return;
      chart.innerHTML = '<p class="text-muted">Karşılaştırmalı metrik geçmişi yükleniyor...</p>';
      const histories = await Promise.all(runs.map(run => fetchJson(`/api/mlflow/runs/${encodeURIComponent(run.run_id)}/metrics/${encodeURIComponent(metric)}/history`)));
      renderLineChart(chart, histories.map((history, index) => ({name:runs[index].run_name || runs[index].run_id, points:history.points})), metric);
    }
    metricSelect.addEventListener("change", () => loadComparisonChart().catch(error => { chart.textContent = error.message; }));
    fetchJson(`/api/mlflow/runs/compare?${query}`).then(data => {
      runs = data.runs || [];
      const keys = [...new Set(runs.flatMap(run => [
        ...Object.keys(run.params_map || {}).map(key => `P:${key}`),
        ...Object.keys(run.metrics_map || {}).map(key => `M:${key}`),
      ]))].sort();
      content.innerHTML = `<table class="table"><thead><tr><th>Alan</th>${runs.map(run => `<th><a href="/runs/${encodeURIComponent(run.run_id)}">${esc(run.run_name)}</a></th>`).join("")}</tr></thead><tbody>${keys.map(key => {
        const [kind, ...rest] = key.split(":"); const name = rest.join(":");
        const values = runs.map(run => (kind === "P" ? run.params_map : run.metrics_map)?.[name] ?? "—");
        return `<tr><th><span class="badge badge-neutral">${kind === "P" ? "Parametre" : "Metrik"}</span> ${esc(name)}</th>${values.map(value => `<td><code>${esc(value)}</code></td>`).join("")}</tr>`;
      }).join("")}</tbody></table>`;
      const metricKeys = [...new Set(runs.flatMap(run => Object.keys(run.metrics_map || {})))].sort();
      metricSelect.innerHTML = metricKeys.map(key => `<option value="${esc(key)}">${esc(key)}</option>`).join("");
      status.textContent = ""; content.hidden = false;
      if (metricKeys.length) loadComparisonChart().catch(error => { chart.textContent = error.message; });
    }).catch(error => { status.textContent = error.message; status.className = "text-destructive"; });
  }

  initDashboard();
  initExperimentDetail();
  initRunDetail();
  initRunCompare();
})();
