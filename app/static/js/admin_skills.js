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
  const kilobytes = length => `${(length / 1024).toFixed(1)} KB`;
  const formatDate = value => {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("tr-TR");
  };

  function initSkillLibrary() {
    const form = document.getElementById("skill-form");
    const list = document.getElementById("skill-list");
    if (!form || !list) return;
    const status = document.getElementById("skill-form-status");
    const usage = document.getElementById("skill-library-usage");
    const saveButton = document.getElementById("btn-save-skill");
    const cancelButton = document.getElementById("btn-cancel-skill");
    const fileInput = document.getElementById("skill-file");
    let skills = [];
    let editing = null;

    const resetForm = () => {
      form.reset();
      editing = null;
      saveButton.textContent = "Skill Ekle";
      cancelButton.hidden = true;
    };
    cancelButton.addEventListener("click", () => { resetForm(); setStatus(status, ""); });

    fileInput.addEventListener("change", async () => {
      const file = fileInput.files && fileInput.files[0];
      if (!file) return;
      form.elements.content.value = await file.text();
      fileInput.value = "";
    });

    // One row per skill; the description wraps under the name instead of
    // squeezing a five-column table into the admin content width.
    const row = skill => `<li class="skill-item">
      <div class="skill-item-main">
        <div class="skill-item-head">
          <code class="skill-item-name">${esc(skill.name)}</code>
          <span class="badge ${skill.enabled ? "badge-running" : "badge-stopped"}">${skill.enabled ? "Etkin" : "Devre Dışı"}</span>
          <small class="text-muted">Güncellendi: ${esc(formatDate(skill.updated_at))}</small>
        </div>
        <p class="skill-item-description" title="${esc(skill.description)}">${esc(skill.description)}</p>
      </div>
      <div class="skill-item-actions">
        <button type="button" class="btn btn-secondary btn-sm" data-skill-action="edit" data-id="${skill.id}">Düzenle</button>
        <button type="button" class="btn btn-secondary btn-sm" data-skill-action="toggle" data-id="${skill.id}">${skill.enabled ? "Devre Dışı Bırak" : "Etkinleştir"}</button>
        <button type="button" class="btn btn-danger btn-sm" data-skill-action="delete" data-id="${skill.id}">Sil</button>
      </div>
    </li>`;

    const load = async () => {
      const data = await request("/api/admin/skills");
      skills = data.skills || [];
      const enabled = skills.filter(skill => skill.enabled).length;
      usage.textContent = `${enabled} etkin · ${kilobytes(data.bundle_length)} / ${kilobytes(data.bundle_limit)}`;
      usage.className = `badge ${enabled ? "badge-running" : "badge-stopped"}`;
      list.innerHTML = skills.length
        ? `<ul class="skill-list">${skills.map(row).join("")}</ul>`
        : '<p class="text-muted">Henüz skill eklenmedi.</p>';
    };

    form.addEventListener("submit", async event => {
      event.preventDefault();
      saveButton.disabled = true;
      setStatus(status, "Kaydediliyor...");
      try {
        const body = JSON.stringify({content: form.elements.content.value, enabled: form.elements.enabled.checked});
        const saved = await request(
          editing ? `/api/admin/skills/${editing.id}` : "/api/admin/skills",
          {method: editing ? "PUT" : "POST", body},
        );
        resetForm();
        await load();
        setStatus(status, `${saved.name} kaydedildi; yeni ve yeniden başlatılan workspace'lere uygulanacak.`, "success");
      } catch (error) {
        setStatus(status, error.message, "error");
      } finally {
        saveButton.disabled = false;
      }
    });

    list.addEventListener("click", async event => {
      const button = event.target.closest("[data-skill-action]");
      if (!button) return;
      const skill = skills.find(item => String(item.id) === button.dataset.id);
      if (!skill) return;
      const action = button.dataset.skillAction;
      if (action === "edit") {
        editing = skill;
        form.elements.content.value = skill.content;
        form.elements.enabled.checked = skill.enabled;
        saveButton.textContent = "Güncelle";
        cancelButton.hidden = false;
        setStatus(status, "");
        form.scrollIntoView({behavior: "smooth", block: "start"});
        return;
      }
      if (action === "delete" && !confirm(`${skill.name} silinsin mi?`)) return;
      button.disabled = true;
      try {
        if (action === "toggle") {
          await request(`/api/admin/skills/${skill.id}`, {method: "PUT", body: JSON.stringify({content: skill.content, enabled: !skill.enabled})});
        } else if (action === "delete") {
          await request(`/api/admin/skills/${skill.id}`, {method: "DELETE"});
          if (editing && editing.id === skill.id) resetForm();
        }
        await load();
      } catch (error) {
        button.disabled = false;
        window.alert(error.message);
      }
    });

    load().catch(error => { list.innerHTML = `<p class="text-destructive">${esc(error.message)}</p>`; });
  }

  initSkillLibrary();
})();
