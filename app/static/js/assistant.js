// In-app assistant side panel. The button stays hidden unless an
// administrator enabled the assistant. The conversation lives in
// sessionStorage, so it survives page changes but not the browser tab.
(() => {
  const toggle = document.querySelector("[data-assistant-toggle]");
  const panel = document.getElementById("assistant-panel");
  if (!toggle || !panel) return;

  const list = panel.querySelector("[data-assistant-messages]");
  const form = panel.querySelector("[data-assistant-form]");
  const input = form.querySelector("textarea");
  const sendButton = form.querySelector("button[type=submit]");
  const STATUS_KEY = "aifactory-assistant-status";
  const HISTORY_KEY = "aifactory-assistant-history";
  const OPEN_KEY = "aifactory-assistant-open";
  const STATUS_TTL_MS = 2 * 60 * 1000;
  const MAX_HISTORY = 24;
  const SUGGESTIONS = [
    "Çalışma alanlarımın durumu ne?",
    "Ne kadar kotam kaldı?",
    "GPU'lu bir çalışma alanı açabilir miyim?",
    "Modelimi nasıl yayınlarım?",
  ];
  const ARGUMENT_LABELS = {
    name: "Ad",
    template_id: "Şablon",
    flavor_id: "Kaynak profili",
    description: "Açıklama",
    auto_stop_minutes: "Azami süre (dk)",
    workspace_id: "Çalışma alanı",
  };

  const store = {
    get(key) {
      try { return JSON.parse(sessionStorage.getItem(key)); } catch (error) { return null; }
    },
    set(key, value) {
      try { sessionStorage.setItem(key, JSON.stringify(value)); } catch (error) { /* private mode */ }
    },
  };
  let history = Array.isArray(store.get(HISTORY_KEY)) ? store.get(HISTORY_KEY) : [];
  let busy = false;

  const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (char) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]
  ));

  // A small, safe Markdown subset: fenced code, inline code, bold, bullet
  // lists and links to pages of this application only.
  const inline = (text) => escapeHtml(text)
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([^\]\n]+)\]\((\/(?!\/)[^\s)"<>]*)\)/g, '<a href="$2">$1</a>');
  const render = (text) => {
    const html = [];
    let items = [];
    const flush = () => {
      if (items.length) html.push(`<ul>${items.map((item) => `<li>${item}</li>`).join("")}</ul>`);
      items = [];
    };
    String(text).split(/```/).forEach((part, index) => {
      if (index % 2) {
        flush();
        html.push(`<pre><code>${escapeHtml(part.replace(/^[a-z0-9_-]*\n/i, ""))}</code></pre>`);
        return;
      }
      part.split("\n").forEach((line) => {
        const bullet = line.match(/^\s*[-*•]\s+(.*)$/);
        if (bullet) { items.push(inline(bullet[1])); return; }
        flush();
        if (line.trim()) html.push(`<div>${inline(line.replace(/^#+\s*/, ""))}</div>`);
      });
      flush();
    });
    return html.join("");
  };

  const scrollDown = () => { list.scrollTop = list.scrollHeight; };
  const add = (className, html) => {
    const node = document.createElement("div");
    node.className = className;
    node.innerHTML = html;
    list.appendChild(node);
    scrollDown();
    return node;
  };
  const addMessage = (role, content) => add(
    `assistant-msg assistant-msg--${role}`,
    role === "assistant" ? render(content) : escapeHtml(content),
  );
  const remember = (role, content) => {
    history.push({ role, content });
    history = history.slice(-MAX_HISTORY);
    store.set(HISTORY_KEY, history);
  };

  const showEmpty = () => {
    list.innerHTML = "";
    const node = add(
      "assistant-empty",
      `AI Factory'yi kullanırken yardımcı olurum: çalışma alanları, kotalar, MLflow ve GenAI.<ul>${
        SUGGESTIONS.map((text) => `<li><button type="button" class="assistant-suggestion">${escapeHtml(text)}</button></li>`).join("")
      }</ul>`,
    );
    node.querySelectorAll(".assistant-suggestion").forEach((button) => {
      button.addEventListener("click", () => send(button.textContent));
    });
  };
  const restore = () => {
    if (!history.length) { showEmpty(); return; }
    list.innerHTML = "";
    history.forEach((item) => addMessage(item.role, item.content));
  };

  const setBusy = (value) => {
    busy = value;
    sendButton.disabled = value;
    input.disabled = value;
    if (!value) input.focus();
  };

  const describeResult = (payload) => {
    if (!payload.ok) return `**${payload.title || "Eylem"}** başarısız oldu: ${payload.message || "bilinmeyen hata"}`;
    const result = payload.result || {};
    const name = result.name ? ` — ${result.name}` : "";
    const link = result.detail_page ? ` [Ayrıntılar](${result.detail_page})` : "";
    return `**${payload.title}** tamamlandı${name}.${link}`;
  };

  const addAction = (event) => {
    const rows = Object.entries(event.arguments || {})
      .filter(([, value]) => value !== "" && value !== 0 && value !== null)
      .map(([key, value]) => `<dt>${escapeHtml(ARGUMENT_LABELS[key] || key)}</dt><dd>${escapeHtml(value)}</dd>`)
      .join("");
    const card = add(
      "assistant-action",
      `<strong>${escapeHtml(event.title)}</strong><dl>${rows}</dl>
       <div class="assistant-action-buttons">
         <button type="button" class="btn btn-primary btn-sm" data-confirm>Onayla</button>
         <button type="button" class="btn btn-secondary btn-sm" data-cancel>Vazgeç</button>
       </div>`,
    );
    const finish = (text) => {
      card.classList.add("is-done");
      card.querySelector(".assistant-action-buttons").remove();
      addMessage("assistant", text);
      remember("assistant", text);
    };
    card.querySelector("[data-cancel]").addEventListener("click", () => {
      finish(`**${event.title}** iptal edildi.`);
    });
    card.querySelector("[data-confirm]").addEventListener("click", async (click) => {
      click.target.disabled = true;
      card.querySelector("[data-cancel]").disabled = true;
      try {
        const response = await fetch("/api/assistant/actions", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token: event.token }),
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          finish(`**${event.title}** çalıştırılamadı: ${payload.detail || response.status}`);
          return;
        }
        finish(describeResult({ title: event.title, ...payload }));
        document.dispatchEvent(new CustomEvent("aifactory:assistant-action", { detail: payload }));
      } catch (error) {
        finish(`**${event.title}** çalıştırılamadı: bağlantı hatası.`);
      }
    });
  };

  async function send(text) {
    const content = String(text || "").trim();
    if (!content || busy) return;
    if (!history.length) list.innerHTML = "";
    addMessage("user", content);
    remember("user", content);
    input.value = "";
    setBusy(true);
    const status = add("assistant-status", "Düşünüyor…");
    try {
      const response = await fetch("/api/assistant/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages: history.map(({ role, content: body }) => ({ role, content: body })),
        }),
      });
      if (!response.ok || !response.body) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || `İstek başarısız oldu (${response.status}).`);
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      const handle = (line) => {
        if (!line.trim()) return;
        const event = JSON.parse(line);
        if (event.type === "tool") {
          status.textContent = "Bilgiler alınıyor…";
        } else if (event.type === "action") {
          addAction(event);
        } else if (event.type === "text" && event.text) {
          addMessage("assistant", event.text);
          remember("assistant", event.text);
        } else if (event.type === "error") {
          add("assistant-msg assistant-msg--error", escapeHtml(event.message));
        }
        list.appendChild(status);
      };
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop();
        lines.forEach(handle);
      }
      handle(buffer);
    } catch (error) {
      add("assistant-msg assistant-msg--error", escapeHtml(error.message || "Bağlantı hatası."));
    } finally {
      status.remove();
      setBusy(false);
      scrollDown();
    }
  }

  const setOpen = (open) => {
    panel.classList.toggle("is-open", open);
    panel.setAttribute("aria-hidden", String(!open));
    toggle.setAttribute("aria-expanded", String(open));
    store.set(OPEN_KEY, open);
    if (open) { restore(); input.focus(); }
  };

  toggle.addEventListener("click", () => setOpen(!panel.classList.contains("is-open")));
  panel.querySelector("[data-assistant-close]").addEventListener("click", () => {
    setOpen(false);
    toggle.focus();
  });
  panel.querySelector("[data-assistant-reset]").addEventListener("click", () => {
    history = [];
    store.set(HISTORY_KEY, history);
    showEmpty();
  });
  form.addEventListener("submit", (event) => { event.preventDefault(); send(input.value); });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); send(input.value); }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && panel.classList.contains("is-open") && panel.contains(document.activeElement)) {
      setOpen(false);
      toggle.focus();
    }
  });

  const enable = () => {
    toggle.hidden = false;
    if (store.get(OPEN_KEY) === true) setOpen(true);
  };
  const cached = store.get(STATUS_KEY);
  if (cached && Date.now() - cached.at < STATUS_TTL_MS) {
    if (cached.enabled) enable();
    return;
  }
  fetch("/api/assistant/status")
    .then((response) => (response.ok ? response.json() : { enabled: false }))
    .then((status) => {
      store.set(STATUS_KEY, { enabled: Boolean(status.enabled), at: Date.now() });
      if (status.enabled) enable();
    })
    .catch(() => {});
})();
