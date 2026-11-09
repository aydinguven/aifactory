(() => {
  const C = window.GenAiCharts;
  const $ = (id) => document.getElementById(id);
  const METRIC_LABELS = {total_tokens: "Token", api_requests: "İstek", spend: "Harcama"};
  const MEDALS = ["🥇", "🥈", "🥉"];
  let stats = null;
  let days = 30;
  let metric = "total_tokens";

  const pct = (part, whole) => (whole ? `%${((part / whole) * 100).toFixed(1)}` : "—");
  const value = (row) => Number(row[metric]) || 0;

  // Leaderboard of the top ``limit`` rows. The viewer's own row (``isSelf``) is
  // highlighted and, when outside the top, pinned below with its real rank.
  // ``includeIdle`` lists rows without usage after the ranked ones.
  function board(listId, rows, {name, detail, emptyId, isSelf = () => false, limit = 10, includeIdle = false}) {
    const list = $(listId);
    const ranked = rows.filter((row) => value(row) > 0).sort((a, b) => value(b) - value(a));
    const sorted = ranked.slice(0, limit);
    const entries = sorted.map((row, index) => [row, index]);
    const selfIndex = ranked.findIndex(isSelf);
    if (selfIndex >= limit) entries.push([ranked[selfIndex], selfIndex]);
    if (includeIdle) rows.filter((row) => value(row) <= 0).forEach((row) => entries.push([row, -1]));
    if (emptyId) $(emptyId).hidden = sorted.length > 0;
    const top = sorted.length ? value(sorted[0]) : 1;
    const fmt = C.formatter(metric);
    list.replaceChildren(...entries.map(([row, index]) => {
      const item = document.createElement("li");
      const classes = ["genai-board-row"];
      if (index >= 0 && index < 3) classes.push("is-podium");
      if (isSelf(row)) classes.push("is-self");
      if (index >= limit) classes.push("is-pinned");
      if (index < 0) classes.push("is-idle");
      item.className = classes.join(" ");
      const rank = document.createElement("span");
      rank.className = "genai-board-rank";
      rank.textContent = index < 0 ? "—" : MEDALS[index] || String(index + 1);
      const body = document.createElement("div");
      body.className = "genai-board-body";
      const head = document.createElement("div");
      head.className = "genai-board-head";
      const title = document.createElement("strong");
      title.textContent = name(row);
      const amount = document.createElement("span");
      amount.textContent = fmt(value(row));
      head.append(title, amount);
      const track = document.createElement("div");
      track.className = "genai-board-track";
      const fill = document.createElement("span");
      fill.style.width = `${index < 0 ? 0 : Math.max(2, (value(row) / top) * 100)}%`;
      fill.style.background = C.PALETTE[index % C.PALETTE.length];
      track.append(fill);
      const meta = document.createElement("small");
      meta.textContent = detail(row);
      body.append(head, track, meta);
      item.append(rank, body);
      return item;
    }));
    if (!entries.length && !emptyId) {
      const item = document.createElement("li");
      item.className = "text-muted";
      item.textContent = "Bu dönemde kullanım yok.";
      list.append(item);
    }
  }

  // 1-based rank of the row matching ``predicate`` among rows with usage.
  function rankOf(rows, predicate) {
    const ranked = rows.filter((row) => value(row) > 0).sort((a, b) => value(b) - value(a));
    const index = ranked.findIndex(predicate);
    return {rank: index >= 0 ? index + 1 : null, of: ranked.length};
  }

  const userName = (row) => (row.full_name ? `${row.username} · ${row.full_name}` : row.username);
  const isMe = (row) => row.aifactory_user_id != null && row.aifactory_user_id === stats.viewer.user_id;

  function scopeKpis(prefix, row) {
    const t = stats.totals;
    $(`${prefix}-requests`).textContent = C.compact(row.api_requests);
    $(`${prefix}-requests-share`).textContent = `kurum payı ${pct(row.api_requests, t.api_requests)}`;
    $(`${prefix}-tokens`).textContent = C.compact(row.total_tokens);
    $(`${prefix}-tokens-share`).textContent = `kurum payı ${pct(row.total_tokens, t.total_tokens)}`;
    $(`${prefix}-users`).textContent = String(row.members);
    $(`${prefix}-users-total`).textContent = `${row.member_total} kayıtlı üyeden`;
    if (stats.show_spend) {
      $(`${prefix}-spend`).textContent = C.money(row.spend);
      $(`${prefix}-spend-share`).textContent = `kurum payı ${pct(row.spend, t.spend)}`;
    }
  }

  function renderTeamScope(labels) {
    const team = stats.my_team;
    $("scope-team").hidden = !team;
    if (!team) return;
    $("team-name").textContent = team.name;
    const ownUnitTeam = team.unit && team.unit.toLocaleLowerCase("tr-TR") === team.name.toLocaleLowerCase("tr-TR");
    $("team-meta").textContent = !team.unit ? "Müdürlük bilgisi yok" : ownUnitTeam ? "Müdürlük ekibi" : `${team.unit} müdürlüğü`;
    const {rank, of} = rankOf(stats.groups.filter((row) => row.key), (row) => row.key === team.key);
    $("team-rank").textContent = rank ? `#${rank} / ${of} takım` : "Bu dönemde kullanım yok";
    scopeKpis("team", team);
    C.bars($("chart-team"), {
      labels,
      metric,
      series: [{name: METRIC_LABELS[metric], color: "#0f766e", values: team.daily.map(value)}],
    });
    $("team-users-count").textContent = `${team.members} / ${team.member_total} aktif`;
    board("board-team-users", team.users, {
      name: userName,
      detail: (row) => `${C.compact(row.api_requests)} istek · ${C.compact(row.total_tokens)} token`,
      isSelf: isMe,
      limit: 25,
    });
  }

  function renderUnitScope(labels) {
    const unit = stats.my_unit;
    $("scope-unit").hidden = !unit;
    if (!unit) return;
    $("unit-name").textContent = unit.name;
    $("unit-meta").textContent = `${unit.team_count} takım · ${unit.member_total} kayıtlı üye`;
    const {rank, of} = rankOf(stats.units, (row) => row.key === unit.key);
    $("unit-rank").textContent = rank ? `#${rank} / ${of} müdürlük` : "Bu dönemde kullanım yok";
    scopeKpis("unit", unit);
    const series = unit.team_series.slice(0, 8).map((team, index) => ({
      name: team.name,
      color: C.PALETTE[index % C.PALETTE.length],
      values: team.values.map(value),
    }));
    C.bars($("chart-unit-teams"), {labels, metric, series});
    $("unit-teams-legend").replaceChildren(...series.map((s) => {
      const span = document.createElement("span");
      span.className = "genai-legend-inline";
      const dot = document.createElement("i");
      dot.style.background = s.color;
      span.append(dot, document.createTextNode(s.name));
      return span;
    }));
    $("unit-teams-count").textContent = `${unit.teams.length} takım`;
    board("board-unit-teams", unit.teams, {
      name: (row) => row.name,
      detail: (row) => `${row.members} / ${row.member_total} aktif kullanıcı · ${C.compact(row.api_requests)} istek · ${C.compact(row.total_tokens)} token`,
      isSelf: (row) => row.key === stats.viewer.team_key,
      limit: 50,
      includeIdle: true,
    });
    $("unit-users-count").textContent = `${unit.users.length} aktif kullanıcı`;
    board("board-unit-users", unit.users, {
      name: userName,
      detail: (row) => `${C.compact(row.api_requests)} istek · ${C.compact(row.total_tokens)} token${row.team ? ` · ${row.team}` : ""}`,
      isSelf: isMe,
      limit: 15,
    });
  }

  function render() {
    const t = stats.totals;
    $("kpi-requests").textContent = C.compact(t.api_requests);
    $("kpi-success").textContent = `${pct(t.successful_requests, t.api_requests)} başarılı · ${C.compact(t.failed_requests)} hatalı`;
    $("kpi-tokens").textContent = C.compact(t.total_tokens);
    $("kpi-tokens-split").textContent = `${C.compact(t.prompt_tokens)} girdi · ${C.compact(t.completion_tokens)} çıktı`;
    $("kpi-users").textContent = String(t.active_users);
    $("kpi-per-user").textContent = t.active_users ? `kişi başı ${C.compact(t.total_tokens / t.active_users)} token` : "";
    if (stats.show_spend) {
      $("kpi-spend").textContent = C.money(t.spend);
      $("kpi-spend-day").textContent = `günlük ort. ${C.money(t.spend / stats.days)}`;
    }
    $("genai-stats-range").textContent = `${stats.start} → ${stats.end}`;

    const labels = stats.daily.map((day) => day.date);
    const lineMetric = metric === "api_requests" ? "total_tokens" : "api_requests";
    C.bars($("chart-daily"), {
      labels,
      metric,
      series: [{name: METRIC_LABELS[metric], color: "#d50032", values: stats.daily.map(value)}],
      line: {name: METRIC_LABELS[lineMetric], metric: lineMetric, color: "#2563eb", values: stats.daily.map((day) => day[lineMetric] || 0)},
    });
    $("daily-legend").textContent = `■ ${METRIC_LABELS[metric]}  ─ ${METRIC_LABELS[lineMetric]}`;

    renderUnitScope(labels);
    renderTeamScope(labels);
    $("scope-global-title").hidden = !stats.my_team && !stats.my_unit;

    $("users-count").textContent = `${stats.users.length} kullanıcı`;
    board("board-users", stats.users, {
      name: userName,
      detail: (row) => `${C.compact(row.api_requests)} istek · ${C.compact(row.total_tokens)} token${row.team ? ` · ${row.team}` : ""}`,
      emptyId: stats.user_breakdown ? null : "board-users-empty",
      isSelf: isMe,
    });
    // "Takımsız" (users without a team) is not a team, so it is not ranked.
    board("board-groups", stats.groups.filter((row) => row.key), {
      name: (row) => row.name,
      detail: (row) => `${row.members} aktif kullanıcı · ${C.compact(row.api_requests)} istek${row.unit ? ` · ${row.unit}` : ""}`,
      isSelf: (row) => row.key && row.key === stats.viewer.team_key,
    });
    board("board-units", stats.units || [], {
      name: (row) => row.name,
      detail: (row) => `${row.team_count} takım · ${row.members} aktif kullanıcı · ${C.compact(row.api_requests)} istek`,
      emptyId: "board-units-empty",
      isSelf: (row) => row.key === stats.viewer.unit_key,
    });
    board("board-teams", stats.teams || [], {
      name: (row) => row.name,
      detail: (row) => `${C.compact(row.api_requests)} istek · ${C.compact(row.total_tokens)} token`,
      emptyId: "board-teams-empty",
    });

    const models = stats.models.filter((row) => value(row) > 0).slice(0, 8);
    const rest = stats.models.slice(8).reduce((sum, row) => sum + value(row), 0);
    const items = models.map((row, index) => ({label: row.model, value: value(row), color: C.PALETTE[index]}));
    if (rest > 0) items.push({label: "Diğer", value: rest, color: "#94a3b8"});
    C.donut($("chart-models"), items, {metric});
    const total = items.reduce((sum, item) => sum + item.value, 0);
    $("legend-models").replaceChildren(...items.map((item) => {
      const li = document.createElement("li");
      const dot = document.createElement("i");
      dot.style.background = item.color;
      const label = document.createElement("span");
      label.textContent = item.label;
      const share = document.createElement("b");
      share.textContent = pct(item.value, total);
      li.append(dot, label, share);
      return li;
    }));

    const series = (stats.team_series || []).slice(0, 6).map((team, index) => ({
      name: team.name,
      color: C.PALETTE[index % C.PALETTE.length],
      values: team.values.map(value),
    }));
    $("teams-trend-card").hidden = !series.some((s) => s.values.some(Boolean));
    if (!$("teams-trend-card").hidden) {
      C.bars($("chart-teams"), {labels, metric, series});
      $("teams-legend").replaceChildren(...series.map((s) => {
        const span = document.createElement("span");
        span.className = "genai-legend-inline";
        const dot = document.createElement("i");
        dot.style.background = s.color;
        span.append(dot, document.createTextNode(s.name));
        return span;
      }));
    }
  }

  async function load() {
    $("genai-stats-status").hidden = false;
    $("genai-stats-status").textContent = "İstatistikler yükleniyor...";
    try {
      const response = await fetch(`/api/genai/stats?days=${days}`, {credentials: "same-origin"});
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || `İstek başarısız (${response.status})`);
      if (!result.available) throw new Error(`LiteLLM'den veri alınamadı: ${result.error || "bilinmeyen hata"}`);
      stats = result;
      document.querySelectorAll("[data-admin-only]").forEach((node) => { node.hidden = !stats.show_spend; });
      if (!stats.show_spend && metric === "spend") metric = "total_tokens";
      render();
      $("genai-stats-body").hidden = false;
      $("genai-stats-status").hidden = true;
    } catch (error) {
      $("genai-stats-status").textContent = error.message;
    }
  }

  document.querySelectorAll("[data-days]").forEach((button) => button.addEventListener("click", () => {
    days = Number(button.dataset.days);
    document.querySelectorAll("[data-days]").forEach((b) => b.classList.toggle("active", b === button));
    load();
  }));
  document.querySelectorAll("[data-metric]").forEach((button) => button.addEventListener("click", () => {
    metric = button.dataset.metric;
    document.querySelectorAll("[data-metric]").forEach((b) => b.classList.toggle("active", b === button));
    if (stats) render();
  }));
  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (stats) render(); }, 150);
  });
  load();
})();
