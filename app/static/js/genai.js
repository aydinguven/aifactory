(() => {
  const $ = (id) => document.getElementById(id);
  const sections = ["genai-none", "genai-active", "genai-usage"];
  const money = (value, digits = 4) => (value === null || value === undefined ? "Sınırsız" : `$${Number(value).toFixed(digits)}`);
  const when = (value) => (value ? new Date(value).toLocaleString("tr-TR") : "—");

  function showError(message) {
    const box = $("genai-error");
    box.textContent = message || "";
    box.hidden = !message;
  }

  async function request(url, options = {}) {
    const response = await fetch(url, {credentials: "same-origin", ...options});
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || `İstek başarısız (${response.status})`);
    return result;
  }

  function snippet(baseUrl, key) {
    const root = baseUrl.replace(/\/+$/, "");
    return [
      "# OpenAI uyumlu istemciler",
      `export OPENAI_BASE_URL="${root}/v1"`,
      `export OPENAI_API_KEY="${key}"`,
      "",
      "from openai import OpenAI",
      "client = OpenAI()  # ortam değişkenlerini kullanır",
      "client.models.list()",
      "",
      "# curl",
      `curl ${root}/v1/models -H "Authorization: Bearer ${key}"`,
    ].join("\n");
  }

  function renderIssued(issued) {
    $("genai-issued-key").value = issued.api_key;
    $("genai-snippet").textContent = snippet(issued.base_url, issued.api_key);
    $("genai-issued-warning").textContent = issued.warning || "";
    $("genai-issued-warning").hidden = !issued.warning;
    $("genai-issued").hidden = false;
    $("genai-issued").scrollIntoView({behavior: "smooth", block: "start"});
  }

  function renderStatus(status) {
    sections.forEach((id) => { $(id).hidden = true; });
    showError(status.error ? `LiteLLM'e ulaşılamadı: ${status.error}` : "");
    if (status.provisioned) {
      $("genai-user-id").textContent = status.litellm_user_id;
      $("genai-key-alias").textContent = status.key_alias || "—";
      $("genai-base-url").textContent = status.base_url;
      $("genai-created").textContent = when(status.created_at);
      $("genai-rotated").textContent = when(status.rotated_at);
      $("genai-team").textContent = status.team || "—";
      $("genai-workspace-key").textContent = status.workspace_key
        ? "Kişisel (yeni workspace'lerde kullanılır)"
        : "Henüz yok; ilk yeni workspace'te oluşturulur";
      const teamChanged = status.key_team_current === false;
      $("genai-team-changed").textContent = teamChanged
        ? `Takımınız ${status.key_team || "—"} → ${status.team || "—"} olarak değişti. Yeni takımın limitleri için anahtarınızı yenileyin; yeni workspace'ler otomatik olarak yeni takımı kullanır.`
        : "";
      $("genai-team-changed").hidden = !teamChanged;
      const missing = status.key_active === false || status.litellm_user_exists === false;
      $("genai-key-missing").hidden = !missing;
      $("genai-key-badge").className = `badge ${missing ? "badge-stopped" : "badge-running"}`;
      $("genai-key-badge").textContent = missing ? "Anahtar yok" : "Etkin";
      $("btn-genai-rotate").textContent = missing ? "Yeni anahtar oluştur" : "Anahtarı yenile";
      $("genai-active").hidden = false;
    } else if (!status.error) {
      const existing = status.litellm_user_exists === true;
      $("genai-none-title").textContent = existing ? "LiteLLM kullanıcınız mevcut" : "GenAI erişiminiz yok";
      $("genai-none-text").replaceChildren(
        existing
          ? `LiteLLM'de ${status.litellm_user_id} kullanıcınız zaten var.${status.workspace_key ? " Workspace'leriniz bu kullanıcıya ait anahtarı otomatik kullanır." : ""} Workspace dışında kullanmak için kişisel bir API anahtarı oluşturabilirsiniz.`
          : `Tek tıkla ${status.litellm_user_id} adlı LiteLLM kullanıcınız oluşturulur ve size kişisel bir API anahtarı verilir.`
      );
      $("btn-genai-create").textContent = existing ? "API anahtarı oluştur" : "GenAI erişimi oluştur";
      $("genai-none").hidden = false;
    }
    if (status.usage) {
      $("genai-spend").textContent = money(status.usage.spend);
      $("genai-budget").textContent = money(status.usage.max_budget, 2);
      $("genai-reset").textContent = status.usage.budget_reset_at
        ? when(status.usage.budget_reset_at)
        : (status.usage.budget_duration || "—");
      $("genai-usage").hidden = false;
    }
  }

  async function loadHistory() {
    try {
      const history = await request("/api/genai/usage");
      if (!history.available || !history.days.length) return;
      $("genai-history-rows").replaceChildren(...history.days.slice().reverse().map((day) => {
        const row = document.createElement("tr");
        [day.date, day.api_requests.toLocaleString("tr-TR"), day.total_tokens.toLocaleString("tr-TR"), money(day.spend)]
          .forEach((value) => {
            const cell = document.createElement("td");
            cell.textContent = value;
            row.append(cell);
          });
        return row;
      }));
      $("genai-history").hidden = false;
    } catch (_error) {
      // Daily usage is optional and depends on the LiteLLM version.
    }
  }

  async function load() {
    try {
      const status = await request("/api/genai/account");
      renderStatus(status);
      if (status.provisioned) loadHistory();
    } catch (error) {
      showError(error.message);
    } finally {
      $("genai-loading").hidden = true;
    }
  }

  async function issue(button, url, confirmText) {
    if (confirmText && !window.confirm(confirmText)) return;
    button.disabled = true;
    showError("");
    try {
      const issued = await request(url, {method: "POST"});
      await load();
      renderIssued(issued);
    } catch (error) {
      showError(error.message);
    } finally {
      button.disabled = false;
    }
  }

  $("btn-genai-create").addEventListener("click", (event) => issue(event.currentTarget, "/api/genai/account"));
  $("btn-genai-rotate").addEventListener("click", (event) => issue(
    event.currentTarget,
    "/api/genai/account/rotate",
    "Mevcut anahtarınız geçersiz olacak ve onu kullanan uygulamalar çalışmayı durduracak. Devam edilsin mi?",
  ));
  $("btn-genai-copy").addEventListener("click", async () => {
    const input = $("genai-issued-key");
    const button = $("btn-genai-copy");
    let copied = false;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(input.value);
        copied = true;
      }
    } catch (_error) {
      copied = false;
    }
    if (!copied) {
      // HTTP intranet deployments have no async clipboard API.
      input.select();
      try { copied = document.execCommand("copy"); } catch (_error) { copied = false; }
    }
    button.textContent = copied ? "Kopyalandı" : "Ctrl+C ile kopyalayın";
    setTimeout(() => { button.textContent = "Kopyala"; }, 2500);
  });

  load();
})();
