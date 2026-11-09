// Renders /api/health on the public status page. Administrators receive
// messages and details in the same feed; everyone else only statuses.
(() => {
  const REFRESH_MS = 30000;
  const isAdmin = document.body.dataset.admin === "true";
  const overall = document.getElementById("status-overall");
  const headline = document.getElementById("status-headline");
  const meta = document.getElementById("status-meta");
  const list = document.getElementById("status-components");
  const refreshButton = document.getElementById("status-refresh");

  const STATUS_TEXT = {
    ok: "Çalışıyor",
    degraded: "Sorunlu",
    down: "Kesinti",
    disabled: "Kapalı",
    unknown: "Bilinmiyor",
  };
  const HEADLINE = {
    ok: "Tüm sistemler çalışıyor",
    degraded: "Bazı bileşenlerde sorun var",
    down: "Kesinti var",
  };

  function el(tag, props = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (key === "dataset") Object.assign(node.dataset, value);
      else node[key] = value;
    }
    for (const child of [].concat(children)) {
      if (child == null) continue;
      node.append(child instanceof Node ? child : String(child));
    }
    return node;
  }

  const pill = (status) =>
    el("span", { className: "status-pill", dataset: { status }, textContent: STATUS_TEXT[status] || status });

  function time(iso) {
    return iso ? new Date(iso).toLocaleString("tr-TR") : "—";
  }

  function percent(value) {
    return value == null ? "—" : `%${value}`;
  }

  function ago(seconds) {
    if (seconds == null) return "—";
    if (seconds < 120) return `${seconds} sn önce`;
    if (seconds < 7200) return `${Math.round(seconds / 60)} dk önce`;
    return `${Math.round(seconds / 3600)} sa önce`;
  }

  function table(headers, rows) {
    return el("div", { className: "status-table-wrap" }, el("table", { className: "status-table" }, [
      el("thead", {}, el("tr", {}, headers.map((h) => el("th", { textContent: h })))),
      el("tbody", {}, rows.map((cells) => el("tr", {}, cells.map((c) => el("td", {}, c))))),
    ]));
  }

  function workersTable(nodes) {
    return table(
      ["Worker", "Durum", "Heartbeat", "CPU", "RAM", "Disk", "Container", "GPU", "Podman", "Agent", "Notlar"],
      nodes.map((n) => [
        n.name,
        pill(n.status),
        n.connected ? ago(n.heartbeat_age_seconds) : "bağlı değil",
        percent(n.cpu_percent),
        percent(n.memory_percent),
        percent(n.disk_percent),
        n.active_containers,
        n.gpu.devices ? `${n.gpu.devices - n.gpu.unhealthy}/${n.gpu.devices}` : "—",
        n.podman ? (n.podman.ok ? `v${n.podman.version} (${n.podman.latency_ms} ms)` : "hata") : "—",
        n.agent_version ? `v${n.agent_version}` : "—",
        n.problems.join(" "),
      ]),
    );
  }

  function storageTable(paths) {
    return table(
      ["Alan", "Yol", "Durum", "Kullanım", "Boş", "Not"],
      paths.map((p) => [
        p.label,
        p.path,
        pill(p.status),
        percent(p.used_percent),
        p.free_mb == null ? "—" : `${(p.free_mb / 1024).toFixed(1)} GB`,
        p.message || "",
      ]),
    );
  }

  function tasksTable(tasks) {
    return table(
      ["Görev", "Durum", "Son başarı", "Son hata", "Not"],
      tasks.map((t) => [
        t.label,
        pill(t.status),
        time(t.last_success_at),
        t.last_error_at ? `${time(t.last_error_at)} · ${t.last_error}` : "—",
        t.message,
      ]),
    );
  }

  function detailList(details) {
    const items = [];
    for (const [key, value] of Object.entries(details)) {
      if (value == null || value === "" || (Array.isArray(value) && !value.length)) continue;
      items.push(el("dt", { textContent: key }));
      items.push(el("dd", { textContent: typeof value === "object" ? JSON.stringify(value) : String(value) }));
    }
    return items.length ? el("dl", { className: "status-details-list" }, items) : null;
  }

  function detailsFor(component) {
    const d = component.details || {};
    if (component.key === "workers" && d.nodes && d.nodes.length) return workersTable(d.nodes);
    if (component.key === "storage" && d.paths) return storageTable(d.paths);
    if (component.key === "tasks" && d.tasks && d.tasks.length) return tasksTable(d.tasks);
    return detailList(d);
  }

  function renderComponent(component) {
    const title = el("h2", { className: "status-item-title" }, [
      component.label,
      el("span", { className: "status-item-tag", textContent: component.required ? "temel" : "isteğe bağlı" }),
    ]);
    const latency = component.latency_ms != null ? ` · ${component.latency_ms} ms` : "";
    const children = [
      el("div", { className: "status-item-head" }, [title, pill(component.status)]),
      el("p", { className: "status-item-summary", textContent: `${component.summary}${latency}` }),
    ];
    if (isAdmin && component.message && component.message !== component.summary) {
      children.push(el("p", { className: "status-item-message", textContent: component.message }));
    }
    if (isAdmin) {
      const body = detailsFor(component);
      if (body) {
        const open = component.key === "workers" && component.status !== "ok";
        children.push(el("details", { open }, [el("summary", { textContent: "Ayrıntılar" }), body]));
      }
    }
    return el("li", { className: "status-item", dataset: { status: component.status } }, children);
  }

  function render(report) {
    overall.dataset.status = report.status;
    headline.textContent = HEADLINE[report.status] || report.status;
    const parts = [`Son kontrol: ${time(report.checked_at)}`, `AI Factory v${report.version}`];
    if (report.duration_ms != null) parts.push(`${report.duration_ms} ms`);
    meta.textContent = parts.join(" · ");
    // Keep expanded sections open across refreshes.
    const opened = new Set(
      [...list.querySelectorAll("li")].filter((li) => li.querySelector("details[open]")).map((li) => li.dataset.key),
    );
    list.replaceChildren(
      ...report.components.map((component) => {
        const item = renderComponent(component);
        item.dataset.key = component.key;
        const details = item.querySelector("details");
        if (details && opened.has(component.key)) details.open = true;
        return item;
      }),
    );
  }

  let timer = null;
  async function load(manual = false) {
    clearTimeout(timer);
    refreshButton.disabled = true;
    try {
      const url = manual && isAdmin ? "/api/health?refresh=1" : "/api/health";
      const response = await fetch(url, { credentials: "same-origin", cache: "no-store" });
      if (response.status !== 200 && response.status !== 503) throw new Error(`HTTP ${response.status}`);
      render(await response.json());
    } catch (error) {
      overall.dataset.status = "down";
      headline.textContent = "Durum alınamadı";
      meta.textContent = `Controller yanıt vermiyor (${error.message}).`;
    } finally {
      refreshButton.disabled = false;
      timer = setTimeout(load, REFRESH_MS);
    }
  }

  refreshButton.addEventListener("click", () => load(true));
  load();
})();
