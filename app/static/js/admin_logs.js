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
  const formatDate = value => {
    const date = new Date(value);
    return value && !Number.isNaN(date.getTime()) ? date.toLocaleString("tr-TR") : "";
  };

  const forwardingFields = form => ({
    forward_enabled: form.elements.forward_enabled.checked,
    forward_host: String(form.elements.forward_host.value || "").trim(),
    forward_port: Number(form.elements.forward_port.value),
    forward_protocol: form.elements.forward_protocol.value,
    forward_format: form.elements.forward_format.value,
    forward_tls_verify: form.elements.forward_tls_verify.checked,
    forward_ca_pem: String(form.elements.forward_ca_pem.value || ""),
    forward_audit: form.elements.forward_audit.checked,
    forward_app: form.elements.forward_app.checked,
    forward_app_min_level: form.elements.forward_app_min_level.value,
    forward_access: form.elements.forward_access.checked,
  });

  function initLoggingSettings() {
    const form = document.getElementById("logging-settings-form");
    if (!form) return;
    // TLS verification and the CA bundle apply only to the TLS protocol.
    const tlsFields = form.querySelectorAll("[data-tls-only]");
    const showTlsFields = () => {
      const tls = form.elements.forward_protocol.value === "tls";
      tlsFields.forEach(element => { element.hidden = !tls; });
    };
    form.elements.forward_protocol.addEventListener("change", showTlsFields);
    showTlsFields();
    const status = document.getElementById("logging-form-status");
    const badge = document.getElementById("log-forwarding-status-badge");
    const note = document.getElementById("logging-revision-note");
    const saveButton = document.getElementById("btn-save-logging");
    const testButton = document.getElementById("btn-test-log-forwarding");

    form.addEventListener("submit", async event => {
      event.preventDefault();
      saveButton.disabled = true;
      setStatus(status, "Kaydediliyor...");
      try {
        const saved = await request("/api/admin/logging-settings", {
          method: "PUT",
          body: JSON.stringify({
            retention_days: Number(form.elements.retention_days.value),
            max_total_mb: Number(form.elements.max_total_mb.value),
            log_level: form.elements.log_level.value,
            ...forwardingFields(form),
          }),
        });
        form.dataset.revision = String(saved.revision);
        form.elements.forward_host.value = saved.forward_host;
        form.elements.forward_ca_pem.value = saved.forward_ca_pem;
        badge.className = `badge ${saved.forward_enabled ? "badge-running" : "badge-stopped"}`;
        badge.textContent = saved.forward_enabled ? "Etkin" : "Devre Dışı";
        note.textContent = `Revizyon ${saved.revision} · ${saved.updated_by}. Kaydedilen ayarlar controller'da hemen, worker'larda 30 saniye içinde uygulanır.`;
        setStatus(status, `Kaydedildi (revizyon ${saved.revision}).`, "success");
        refreshStatus();
      } catch (error) {
        setStatus(status, error.message, "error");
      } finally {
        saveButton.disabled = false;
      }
    });

    testButton.addEventListener("click", async () => {
      testButton.disabled = true;
      setStatus(status, "Test olayı gönderiliyor...");
      try {
        const result = await request("/api/admin/logging-settings/test", {
          method: "POST",
          body: JSON.stringify({...forwardingFields(form), forward_enabled: true}),
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

  const rows = document.getElementById("logging-status-rows");

  const statusRow = (label, connected, state, revision) => {
    if (!state || !state.configured) {
      const reason = connected ? "Worker bu sürümde günlük durumu bildirmiyor." : "Çevrim dışı; durum bilinmiyor.";
      return `<tr><td><strong>${esc(label)}</strong></td><td colspan="5" class="text-muted">${esc(reason)}</td></tr>`;
    }
    const current = !state.managed ? revision === 0 : state.revision === revision;
    const revisionBadge = `<span class="badge ${current ? "badge-running" : "badge-starting"}" title="${current ? "Güncel" : "Bekleniyor"}">${state.managed ? esc(state.revision) : "varsayılan"}</span>`;
    const files = state.file_logging
      ? `<span class="badge badge-running">Açık</span> <small class="text-muted">${esc(state.log_dir)} · ${esc(state.retention_days)} gün · ${esc(state.max_total_mb)} MB · ${esc(state.level)}</small>`
      : `<span class="badge badge-stopped">Kapalı</span> <small class="text-muted">${esc(state.file_error || `${state.log_dir} yok`)}</small>`;
    const forwarding = state.forwarding || {};
    let forwardCell = '<span class="badge badge-neutral">Kapalı</span>';
    let counters = "—";
    let lastError = "";
    if (forwarding.enabled) {
      // A forwarder opens its connection with the first record. A worker that
      // has logged nothing to forward yet is waiting, not disconnected.
      const udp = String(forwarding.destination || "").endsWith("/udp");
      let state = ["badge-error", "Bağlantı yok", "Son gönderim denemesi başarısız oldu."];
      if (forwarding.connected) {
        state = ["badge-running", udp ? "Gönderiyor" : "Bağlı", udp ? "UDP teslimatı doğrulanamaz; kayıtlar gönderildi." : "Toplayıcıya bağlı."];
      } else if (!forwarding.last_error && !forwarding.sent) {
        state = ["badge-neutral", "Bekliyor", "Henüz yönlendirilecek kayıt oluşmadı; bağlantı ilk kayıtla açılır."];
      }
      forwardCell = `<span class="badge ${state[0]}" title="${esc(state[2])}">${state[1]}</span> <small class="text-muted">${esc(forwarding.destination)} · ${esc(forwarding.format)}</small>`;
      counters = `${esc(forwarding.sent)} / ${esc(forwarding.queued)} / ${esc(forwarding.dropped)}`;
      if (forwarding.last_error) lastError = `${esc(forwarding.last_error)}<br><small class="text-muted">${esc(formatDate(forwarding.last_error_at))}</small>`;
    }
    return `<tr><td><strong>${esc(label)}</strong></td><td>${revisionBadge}</td><td>${files}</td><td>${forwardCell}</td><td style="text-align:right;">${counters}</td><td>${lastError}</td></tr>`;
  };

  async function refreshStatus() {
    if (!rows) return;
    try {
      const data = await request("/api/admin/logging-status");
      const html = [statusRow("Controller", true, data.controller, data.revision)];
      for (const worker of data.workers) {
        const label = `${worker.name}${worker.hostname && worker.hostname !== worker.name ? ` (${worker.hostname})` : ""}`;
        html.push(statusRow(label, worker.connected, worker.connected ? worker.logging : null, data.revision));
      }
      rows.innerHTML = html.join("");
    } catch (error) {
      rows.innerHTML = `<tr><td colspan="6" class="text-destructive">${esc(error.message)}</td></tr>`;
    }
  }

  initLoggingSettings();
  refreshStatus();
  setInterval(() => { if (!document.hidden) refreshStatus(); }, 15000);
})();
