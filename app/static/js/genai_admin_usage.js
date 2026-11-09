// Fill admin user cards, teams and müdürlüks with LiteLLM usage (30 days).
(() => {
  const userBlocks = document.querySelectorAll("[data-genai-user]");
  const groupBlocks = document.querySelectorAll("[data-genai-group]");
  const unitBlocks = document.querySelectorAll("[data-genai-unit]");
  if (!userBlocks.length && !groupBlocks.length && !unitBlocks.length) return;
  const C = window.GenAiCharts;

  async function load() {
    let stats;
    try {
      const response = await fetch("/api/genai/stats?days=30", {credentials: "same-origin"});
      stats = await response.json();
      if (!response.ok || !stats.available) return; // GenAI not configured or LiteLLM down
    } catch (_error) {
      return;
    }
    const ranked = stats.users.slice().sort((a, b) => b.total_tokens - a.total_tokens);
    const byUser = new Map(stats.users.filter((row) => row.aifactory_user_id).map((row) => [String(row.aifactory_user_id), row]));
    const byGroup = new Map(stats.groups.map((row) => [row.key, row]));

    // Compact user rows: tokens, rank and a sparkline; details in the tooltip.
    userBlocks.forEach((block) => {
      const row = byUser.get(block.dataset.genaiUser);
      const set = (name, text) => {
        const node = block.querySelector(`[data-llm="${name}"]`);
        if (node) node.textContent = text;
      };
      if (row && row.total_tokens) {
        set("total_tokens", C.compact(row.total_tokens));
        set("rank", `#${ranked.indexOf(row) + 1}/${ranked.length}`);
        block.title = `Son 30 gün: ${C.compact(row.api_requests)} istek · girdi ${C.compact(row.prompt_tokens)} / ` +
          `çıktı ${C.compact(row.completion_tokens)}${stats.show_spend ? ` · ${C.money(row.spend)}` : ""}`;
        block.querySelector('[data-llm="spark"]')?.replaceChildren(
          C.sparkline(row.daily_tokens, {width: 120, height: 16})
        );
        block.classList.remove("is-idle");
      } else {
        set("total_tokens", "—");
        set("rank", "kullanım yok");
        block.classList.add("is-idle");
      }
    });

    const fillUsage = (block, row) => {
      if (!row || !row.api_requests) return;
      const label = document.createElement("span");
      label.textContent = `${C.compact(row.total_tokens)} token`;
      block.title = `Son 30 gün: ${C.compact(row.total_tokens)} token · ${C.compact(row.api_requests)} istek` +
        `${stats.show_spend ? ` · ${C.money(row.spend)}` : ""}`;
      block.replaceChildren(C.sparkline(row.daily_tokens, {width: 56, height: 14}), label);
      block.hidden = false;
    };
    const byUnit = new Map((stats.units || []).map((row) => [row.key, row]));
    groupBlocks.forEach((block) => fillUsage(block, byGroup.get(block.dataset.genaiGroup)));
    unitBlocks.forEach((block) => fillUsage(block, byUnit.get(block.dataset.genaiUnit)));
  }

  load();
})();
