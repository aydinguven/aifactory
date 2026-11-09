(() => {
  "use strict";

  if (document.body.dataset.onboardingUser !== "true") return;

  const TOPIC_TITLES = {
    "workspace-create": "Çalışma alanı oluşturma",
    "resource-usage": "Kaynak ve kota görünümü",
    "workspace-detail": "Çalışma alanı ayrıntıları",
    mlflow: "MLflow",
    profile: "Kurumsal profil",
    admin: "Yönetim paneli",
  };

  const dashboardRoute = () => "/";
  const detailRoute = currentState => currentState.context.first_workspace_url || "/";
  const detailSelector = (currentState, realSelector, demoSelector) => (
    currentState.context.first_workspace_url ? realSelector : demoSelector
  );

  const STEPS = [
    {
      id: "ws-create-open",
      topic: "workspace-create",
      title: "Yeni çalışma alanı",
      detail: "Yeni bir geliştirme ortamı oluşturma akışı bu düğmeyle başlar.",
      route: dashboardRoute,
      selector: '[data-tour="workspace-create-button"]',
    },
    {
      id: "ws-create-dialog",
      topic: "workspace-create",
      group: "workspace-create-modal",
      title: "Oluşturma formu",
      detail: "Bu form çalışma alanının adını, çalışma süresini, şablonunu ve kaynak profilini bir arada toplar. Tur formu göndermez.",
      route: dashboardRoute,
      selector: '[data-tour="workspace-create-dialog"]',
      prepare: prepareCreateModal,
      cleanup: cleanupCreateModal,
    },
    {
      id: "ws-create-name",
      topic: "workspace-create",
      group: "workspace-create-modal",
      title: "Ad ve açıklama",
      detail: "Çalışma alanınıza ayırt edilebilir bir ad verin; açıklama ekip içinde amacını hatırlatır.",
      route: dashboardRoute,
      selector: '[data-tour="workspace-name"]',
      prepare: prepareCreateModal,
      cleanup: cleanupCreateModal,
    },
    {
      id: "ws-create-template",
      topic: "workspace-create",
      group: "workspace-create-modal",
      title: "Geliştirme şablonu",
      detail: "Kullanacağınız IDE ve runtime paketini buradan seçersiniz.",
      route: dashboardRoute,
      selector: '[data-tour="template-picker"]',
      prepare: prepareCreateModal,
      cleanup: cleanupCreateModal,
    },
    {
      id: "ws-create-flavor",
      topic: "workspace-create",
      group: "workspace-create-modal",
      title: "Kaynak profili",
      detail: "CPU, RAM veya uygun olduğunda GPU kapasitesini ihtiyacınıza göre seçin.",
      route: dashboardRoute,
      selector: '[data-tour="flavor-picker"]',
      prepare: prepareCreateModal,
      cleanup: cleanupCreateModal,
    },
    {
      id: "ws-create-submit",
      topic: "workspace-create",
      group: "workspace-create-modal",
      title: "Kurulumu başlatma",
      detail: "Seçimler tamamlandığında bu düğme kurulumu başlatır. Tur bu düğmeye basmaz ve kaynak oluşturmaz.",
      route: dashboardRoute,
      selector: '[data-tour="workspace-create-submit"]',
      prepare: prepareCreateModal,
      cleanup: cleanupCreateModal,
    },
    {
      id: "dashboard-quota",
      topic: "resource-usage",
      title: "Kaynaklar ve kotalar",
      detail: "Sistem kapasitesini, hesabınıza ayrılan kullanımı ve yeni çalışma alanları için kalan kotayı birlikte izleyin.",
      route: dashboardRoute,
      selector: '[data-tour="quota-summary"]',
    },
    {
      id: "ws-detail-overview",
      topic: "workspace-detail",
      group: "workspace-detail",
      title: "Workspace genel görünümü",
      detail: currentState => currentState.context.first_workspace_url
        ? "Workspace yaşam döngüsü ve IDE işlemleri bu alanda bulunur."
        : "Bu açıkça işaretlenmiş demo gerçek bir workspace oluşturmaz; gerçek ayrıntı ekranının yapısını örnekler.",
      route: detailRoute,
      selector: currentState => detailSelector(
        currentState,
        '[data-tour="workspace-actions"]',
        '[data-tour="workspace-demo-overview"]',
      ),
      prepare: prepareWorkspaceDetail,
      cleanup: cleanupWorkspaceDetail,
    },
    {
      id: "ws-detail-metrics",
      topic: "workspace-detail",
      group: "workspace-detail",
      title: "Canlı metrikler",
      detail: currentState => currentState.context.first_workspace_url
        ? "CPU, RAM, disk ve çalışma süresi değerleri burada güncellenir."
        : "Demo metrikleri yalnızca görünümü anlatır; worker veya kota kullanmaz.",
      route: detailRoute,
      selector: currentState => detailSelector(
        currentState,
        '[data-tour="workspace-metrics"]',
        '[data-tour="workspace-demo-metrics"]',
      ),
      prepare: prepareWorkspaceDetail,
      cleanup: cleanupWorkspaceDetail,
    },
    {
      id: "ws-detail-logs",
      topic: "workspace-detail",
      group: "workspace-detail",
      title: "Container logları",
      detail: "Container çıktısını ve çalışma zamanı hatalarını bu sekmeden izleyebilirsiniz.",
      route: detailRoute,
      selector: currentState => detailSelector(
        currentState,
        '[data-tour="workspace-tab-logs"]',
        '[data-tour="workspace-demo-logs"]',
      ),
      prepare: prepareWorkspaceDetail,
      cleanup: cleanupWorkspaceDetail,
    },
    {
      id: "ws-detail-files",
      topic: "workspace-detail",
      group: "workspace-detail",
      title: "Dosya yöneticisi",
      detail: "Kalıcı workspace dosyalarına tarayıcıdan erişmek için bu sekmeyi kullanın.",
      route: detailRoute,
      selector: currentState => detailSelector(
        currentState,
        '[data-tour="workspace-tab-files"]',
        '[data-tour="workspace-demo-files"]',
      ),
      prepare: prepareWorkspaceDetail,
      cleanup: cleanupWorkspaceDetail,
    },
    {
      id: "ws-detail-ports",
      topic: "workspace-detail",
      group: "workspace-detail",
      title: "Port önizleme",
      detail: "Workspace içinde çalıştırdığınız web uygulamalarını seçilen port üzerinden burada açabilirsiniz.",
      route: detailRoute,
      selector: currentState => detailSelector(
        currentState,
        '[data-tour="workspace-tab-ports"]',
        '[data-tour="workspace-demo-ports"]',
      ),
      prepare: prepareWorkspaceDetail,
      cleanup: cleanupWorkspaceDetail,
    },
    {
      id: "mlflow-nav",
      topic: "mlflow",
      optionalSection: true,
      title: "MLflow bölümleri",
      detail: "Deneyler, run'lar, model registry ve deployment alanları bu sekmelerde gruplanır.",
      route: () => "/models",
      selector: '[data-tour="mlflow-nav"]',
    },
    {
      id: "mlflow-connection",
      topic: "mlflow",
      title: "Kişisel MLflow bağlantısı",
      detail: "Yönetici sunucuyu etkinleştirdikten sonra kendi kimlik bilgilerinizi bu alanda kaydedebilirsiniz.",
      route: () => "/models",
      selector: '[data-tour="mlflow-personal-settings"]',
    },
    {
      id: "profile-details",
      topic: "profile",
      title: "Kurumsal profil bilgileri",
      detail: "Kimlik ve organizasyon bilgileriniz burada gösterilir; LDAP kullanıcılarında başarılı girişlerde güncellenir.",
      route: () => "/profile",
      selector: '[data-tour="profile-details"]',
    },
    {
      id: "profile-tour-control",
      topic: "profile",
      title: "Turu yeniden başlatma",
      detail: "Canlı turu daha sonra yeniden başlatmak veya duraklatılan yerden devam etmek için bu düğmeyi kullanın.",
      route: () => "/profile",
      selector: '[data-tour="tour-restart-profile"]',
    },
    {
      id: "admin-summary",
      topic: "admin",
      title: "Sistem özeti",
      detail: "Yönetici hesapları kullanıcı, workspace, container ve worker sayılarını burada hızlıca görür.",
      route: () => "/admin",
      selector: '[data-tour="admin-summary"]',
    },
    {
      id: "admin-sections",
      topic: "admin",
      title: "Yönetim alanları",
      detail: "Kullanıcılar, workspace katalogları, worker'lar, entegrasyonlar ve sistem ayarları ayrı kategorilerde bulunur.",
      route: () => "/admin",
      selector: '[data-tour="admin-overview"]',
    },
  ];

  const LEGACY_STEP = {choice: true, highlight: true, "": true};
  let state = null;
  let closeDialog = null;
  let closeHighlight = null;
  let originalWorkspaceTab = null;
  const restartButtons = [...document.querySelectorAll("[data-onboarding-restart]")];
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function prepareCreateModal() {
    window.AIFactoryWorkspaceCreateModal?.open({resetDeploymentUI: false});
  }

  function cleanupCreateModal(_currentState, nextStep) {
    if (nextStep?.group === "workspace-create-modal") return;
    window.AIFactoryWorkspaceCreateModal?.close({force: true});
  }

  function prepareWorkspaceDetail(currentState, step) {
    if (!currentState.context.first_workspace_url) {
      const demo = document.querySelector("[data-onboarding-demo-workspace]");
      demo?.removeAttribute("hidden");
      const demoTab = {
        "ws-detail-logs": "workspace-demo-logs",
        "ws-detail-files": "workspace-demo-files",
        "ws-detail-ports": "workspace-demo-ports",
      }[step.id];
      if (demoTab) {
        demo?.querySelectorAll(".onboarding-demo-tabs [data-tour^='workspace-demo-']").forEach(element => {
          element.classList.toggle("active", element.dataset.tour === demoTab);
        });
      }
      return;
    }
    const tabTarget = {
      "ws-detail-logs": "workspace-tab-logs",
      "ws-detail-files": "workspace-tab-files",
      "ws-detail-ports": "workspace-tab-ports",
    }[step.id];
    if (!tabTarget) return;
    if (!originalWorkspaceTab) {
      originalWorkspaceTab = document.querySelector(".workspace-tabs-nav .tab-btn.active")?.dataset.tab || "tab-logs";
    }
    const button = document.querySelector(`[data-tour="${tabTarget}"]`);
    if (button && !button.classList.contains("active")) button.click();
  }

  function cleanupWorkspaceDetail(currentState, nextStep) {
    if (nextStep?.group === "workspace-detail") return;
    if (!currentState.context.first_workspace_url) {
      const demo = document.querySelector("[data-onboarding-demo-workspace]");
      demo?.setAttribute("hidden", "");
      demo?.querySelectorAll(".onboarding-demo-tabs .tab-btn").forEach((element, index) => {
        element.classList.toggle("active", index === 0);
      });
      return;
    }
    if (originalWorkspaceTab) {
      document.querySelector(`.workspace-tabs-nav .tab-btn[data-tab="${originalWorkspaceTab}"]`)?.click();
      originalWorkspaceTab = null;
    }
  }

  const fetchJson = async (url, options = {}) => {
    const response = await fetch(url, {
      cache: "no-store",
      ...options,
      headers: {
        ...(options.body ? {"Content-Type": "application/json"} : {}),
        ...(options.headers || {}),
      },
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.detail || `Tur işlemi başarısız (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return data;
  };

  const refreshState = async () => {
    state = await fetchJson("/api/onboarding/state");
    syncRestartButtons();
    return state;
  };

  const patchState = async update => {
    try {
      state = await fetchJson("/api/onboarding/state", {
        method: "PATCH",
        body: JSON.stringify({
          tour_version: state.tour_version,
          expected_revision: state.revision,
          ...update,
        }),
      });
      syncRestartButtons();
      return state;
    } catch (error) {
      if (error.status === 409) {
        error.onboardingConflict = true;
        await refreshState().catch(() => {});
      }
      throw error;
    }
  };

  const topicAvailable = topic => (
    state.features[topic] !== false && state.topic_choices[topic] !== "skip"
  );
  const availableSteps = () => STEPS.filter(step => topicAvailable(step.topic));
  const firstAvailableStep = () => availableSteps()[0] || null;
  const firstStepForTopic = topic => availableSteps().find(step => step.topic === topic) || null;
  const adjacentStep = (step, direction) => {
    const steps = availableSteps();
    const index = steps.findIndex(item => item.id === step.id);
    return index < 0 ? null : steps[index + direction] || null;
  };
  const nextStepAfterTopic = topic => {
    const topicEnd = STEPS.reduce((last, step, index) => step.topic === topic ? index : last, -1);
    return STEPS.slice(topicEnd + 1).find(step => topicAvailable(step.topic)) || null;
  };

  const resolvePersistedStep = () => {
    const exact = availableSteps().find(step => step.id === state.current_step);
    if (exact) return exact;
    if (state.current_topic) {
      const topicStep = firstStepForTopic(state.current_topic);
      if (topicStep && (LEGACY_STEP[state.current_step] || !state.current_step)) return topicStep;
      if (state.topic_choices[state.current_topic] === "skip" || state.features[state.current_topic] === false) {
        return nextStepAfterTopic(state.current_topic);
      }
      if (topicStep) return topicStep;
    }
    return firstAvailableStep();
  };

  const syncRestartButtons = () => {
    restartButtons.forEach(button => {
      button.hidden = !state?.enabled;
      if (!state?.enabled) return;
      const compact = button.hasAttribute("data-onboarding-compact");
      if (state.status === "paused" || state.status === "in_progress") {
        button.textContent = compact ? "Tura Devam" : "Canlı Tura Devam Et";
      } else if (state.status === "completed") {
        button.textContent = compact ? "Turu Tekrarla" : "Canlı Turu Yeniden Başlat";
      } else {
        button.textContent = compact ? "Tur" : "Canlı Turu Başlat";
      }
    });
  };

  const closeSurfaces = nextStep => {
    closeHighlight?.(nextStep);
    closeHighlight = null;
    closeDialog?.();
    closeDialog = null;
  };

  const trapFocus = (container, event) => {
    if (event.key !== "Tab") return;
    const focusable = [...container.querySelectorAll(
      'button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])'
    )].filter(element => !element.hidden);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable.at(-1);
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const makeBackgroundInert = exceptions => {
    const exceptionSet = new Set(exceptions);
    const changed = [];
    [...document.body.children].forEach(element => {
      if (exceptionSet.has(element) || element.tagName === "SCRIPT") return;
      changed.push([element, element.inert]);
      element.inert = true;
    });
    return () => changed.forEach(([element, previous]) => { element.inert = previous; });
  };

  const openDialog = ({eyebrow, title, body, buttons}) => {
    closeSurfaces(null);
    const previousFocus = document.activeElement;
    let restoreBackground = () => {};
    const backdrop = document.createElement("div");
    backdrop.className = "onboarding-tour-dialog-backdrop";
    const dialog = document.createElement("section");
    dialog.className = "onboarding-tour-dialog";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    const titleId = `onboarding-title-${Date.now()}`;
    const bodyId = `onboarding-body-${Date.now()}`;
    dialog.setAttribute("aria-labelledby", titleId);
    dialog.setAttribute("aria-describedby", bodyId);

    const eyebrowElement = document.createElement("span");
    eyebrowElement.className = "onboarding-tour-eyebrow";
    eyebrowElement.textContent = eyebrow;
    const heading = document.createElement("h2");
    heading.id = titleId;
    heading.textContent = title;
    const paragraph = document.createElement("p");
    paragraph.id = bodyId;
    paragraph.textContent = body;
    const error = document.createElement("p");
    error.className = "onboarding-tour-error";
    error.setAttribute("role", "alert");
    error.hidden = true;
    const actions = document.createElement("div");
    actions.className = "onboarding-tour-actions";

    let closed = false;
    const keyHandler = event => trapFocus(dialog, event);
    const close = () => {
      if (closed) return;
      closed = true;
      backdrop.remove();
      restoreBackground();
      document.body.classList.remove("onboarding-tour-active");
      document.removeEventListener("keydown", keyHandler, true);
      if (previousFocus instanceof HTMLElement && previousFocus.isConnected) previousFocus.focus();
      if (closeDialog === close) closeDialog = null;
    };
    const run = async action => {
      actions.querySelectorAll("button").forEach(button => { button.disabled = true; });
      error.hidden = true;
      try {
        await action(close);
      } catch (actionError) {
        error.textContent = actionError.message;
        error.hidden = false;
        actions.querySelectorAll("button").forEach(button => { button.disabled = false; });
      }
    };
    buttons.forEach(config => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = config.className || "btn btn-secondary";
      button.textContent = config.label;
      if (config.primary) button.dataset.primary = "true";
      button.addEventListener("click", () => run(config.action));
      actions.appendChild(button);
    });

    dialog.append(eyebrowElement, heading, paragraph, error, actions);
    backdrop.appendChild(dialog);
    document.body.appendChild(backdrop);
    restoreBackground = makeBackgroundInert([backdrop]);
    document.body.classList.add("onboarding-tour-active");
    document.addEventListener("keydown", keyHandler, true);
    closeDialog = close;
    (actions.querySelector("[data-primary]") || actions.querySelector("button"))?.focus();
  };

  const waitForTarget = async (selector, timeout = 3500) => {
    const started = performance.now();
    while (performance.now() - started < timeout) {
      const target = document.querySelector(selector);
      if (target && target.getClientRects().length) return target;
      await new Promise(resolve => window.setTimeout(resolve, 100));
    }
    return null;
  };

  const pauseTour = async () => {
    if (state.enabled && state.status !== "completed") await patchState({status: "paused"});
    closeSurfaces(null);
  };

  const finishTour = async () => {
    closeSurfaces(null);
    await patchState({status: "completed", current_topic: "", current_step: ""});
    openDialog({
      eyebrow: "Tur tamamlandı",
      title: "Hazırsınız",
      body: "AI Factory'nin temel alanlarını tamamladınız. Turu üst menüden veya Profil sayfasından istediğiniz zaman yeniden başlatabilirsiniz.",
      buttons: [{label: "Kapat", className: "btn btn-primary", primary: true, action: async close => close()}],
    });
  };

  const persistAndRender = async (step, extra = {}) => {
    if (!step) return finishTour();
    await patchState({
      status: "in_progress",
      current_topic: step.topic,
      current_step: step.id,
      ...extra,
    });
    await renderStep(step);
  };

  const moveFrom = async (step, direction) => {
    const destination = adjacentStep(step, direction);
    if (!destination && direction > 0) return finishTour();
    if (!destination) return;
    const topicChoice = step.optionalSection && !state.topic_choices[step.topic]
      ? {topic_id: step.topic, choice: "show"}
      : undefined;
    closeSurfaces(destination);
    await persistAndRender(destination, topicChoice ? {topic_choice: topicChoice} : {});
  };

  const skipOptionalSection = async step => {
    const destination = nextStepAfterTopic(step.topic);
    closeSurfaces(destination);
    if (!destination) {
      await patchState({topic_choice: {topic_id: step.topic, choice: "skip"}});
      return finishTour();
    }
    await persistAndRender(destination, {
      topic_choice: {topic_id: step.topic, choice: "skip"},
    });
  };

  const createHighlight = (target, step) => {
    closeSurfaces(step);
    const previousFocus = document.activeElement;
    const previousDescription = target.getAttribute("aria-describedby");
    const previousTabIndex = target.getAttribute("tabindex");
    const targetWasFocusable = target.matches('button, a[href], input, select, textarea, [tabindex]');
    if (!targetWasFocusable) target.setAttribute("tabindex", "-1");
    let restoreBackground = () => {};
    const descriptionId = `onboarding-highlight-${Date.now()}`;
    target.classList.add("onboarding-tour-target");
    target.setAttribute("aria-describedby", [previousDescription, descriptionId].filter(Boolean).join(" "));

    const scrims = Array.from({length: 4}, () => {
      const element = document.createElement("div");
      element.className = "onboarding-tour-scrim";
      document.body.appendChild(element);
      return element;
    });
    const callout = document.createElement("section");
    callout.className = "onboarding-tour-callout";
    callout.setAttribute("role", "dialog");
    callout.setAttribute("aria-modal", "true");
    const titleId = `onboarding-callout-title-${Date.now()}`;
    callout.setAttribute("aria-labelledby", titleId);
    callout.setAttribute("aria-describedby", descriptionId);
    const steps = availableSteps();
    const stepNumber = steps.findIndex(item => item.id === step.id) + 1;
    const eyebrow = document.createElement("span");
    eyebrow.className = "onboarding-tour-eyebrow";
    eyebrow.textContent = `Adım ${stepNumber} / ${steps.length} · ${TOPIC_TITLES[step.topic]}`;
    const heading = document.createElement("h2");
    heading.id = titleId;
    heading.textContent = step.title;
    const body = document.createElement("p");
    body.id = descriptionId;
    body.textContent = typeof step.detail === "function" ? step.detail(state) : step.detail;
    const progress = document.createElement("div");
    progress.className = "onboarding-tour-progress";
    progress.setAttribute("aria-hidden", "true");
    const progressValue = document.createElement("span");
    progressValue.style.width = `${Math.max(0, (stepNumber / steps.length) * 100)}%`;
    progress.appendChild(progressValue);
    const actions = document.createElement("div");
    actions.className = "onboarding-tour-actions";

    const pause = document.createElement("button");
    pause.type = "button";
    pause.className = "btn btn-secondary";
    pause.textContent = "Daha Sonra";
    pause.addEventListener("click", () => pauseTour().catch(showFatalError));
    actions.appendChild(pause);

    if (step.optionalSection && !state.topic_choices[step.topic]) {
      const skip = document.createElement("button");
      skip.type = "button";
      skip.className = "btn btn-secondary";
      skip.textContent = `${TOPIC_TITLES[step.topic]} Bölümünü Atla`;
      skip.addEventListener("click", () => skipOptionalSection(step).catch(showFatalError));
      actions.appendChild(skip);
    }

    const previous = adjacentStep(step, -1);
    if (previous) {
      const back = document.createElement("button");
      back.type = "button";
      back.className = "btn btn-secondary";
      back.textContent = "Geri";
      back.addEventListener("click", () => moveFrom(step, -1).catch(showFatalError));
      actions.appendChild(back);
    }

    const next = document.createElement("button");
    next.type = "button";
    next.className = "btn btn-primary";
    next.textContent = adjacentStep(step, 1) ? "Devam" : "Turu Tamamla";
    next.addEventListener("click", () => moveFrom(step, 1).catch(showFatalError));
    actions.appendChild(next);
    callout.append(eyebrow, heading, body, progress, actions);
    document.body.appendChild(callout);
    restoreBackground = makeBackgroundInert([...scrims, callout]);
    document.body.classList.add("onboarding-tour-active");

    let closed = false;
    const update = () => {
      if (closed) return;
      if (!target.isConnected) {
        close(null);
        return;
      }
      const padding = 7;
      const rect = target.getBoundingClientRect();
      const top = Math.max(0, rect.top - padding);
      const left = Math.max(0, rect.left - padding);
      const right = Math.min(window.innerWidth, rect.right + padding);
      const bottom = Math.min(window.innerHeight, rect.bottom + padding);
      Object.assign(scrims[0].style, {left: "0px", top: "0px", width: "100vw", height: `${top}px`});
      Object.assign(scrims[1].style, {left: "0px", top: `${top}px`, width: `${left}px`, height: `${Math.max(0, bottom - top)}px`});
      Object.assign(scrims[2].style, {left: `${right}px`, top: `${top}px`, width: `${Math.max(0, window.innerWidth - right)}px`, height: `${Math.max(0, bottom - top)}px`});
      Object.assign(scrims[3].style, {left: "0px", top: `${bottom}px`, width: "100vw", height: `${Math.max(0, window.innerHeight - bottom)}px`});
      const calloutRect = callout.getBoundingClientRect();
      const calloutLeft = Math.min(Math.max(12, rect.left), Math.max(12, window.innerWidth - calloutRect.width - 12));
      const below = rect.bottom + 16;
      const calloutTop = below + calloutRect.height <= window.innerHeight - 12
        ? below
        : Math.max(12, rect.top - calloutRect.height - 16);
      callout.style.left = `${calloutLeft}px`;
      callout.style.top = `${calloutTop}px`;
    };
    const keyHandler = event => {
      if (event.key === "Escape") {
        event.preventDefault();
        pauseTour().catch(showFatalError);
        return;
      }
      trapFocus(callout, event);
    };
    const close = nextStep => {
      if (closed) return;
      closed = true;
      scrims.forEach(element => element.remove());
      callout.remove();
      target.classList.remove("onboarding-tour-target");
      if (previousDescription) target.setAttribute("aria-describedby", previousDescription);
      else target.removeAttribute("aria-describedby");
      restoreBackground();
      document.body.classList.remove("onboarding-tour-active");
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
      window.removeEventListener("pagehide", close);
      document.removeEventListener("keydown", keyHandler, true);
      observer?.disconnect();
      step.cleanup?.(state, nextStep);
      const focusTarget = target.isConnected && target.getClientRects().length;
      if (focusTarget) target.focus({preventScroll: true});
      else if (previousFocus instanceof HTMLElement && previousFocus.isConnected) previousFocus.focus();
      if (!targetWasFocusable) {
        if (previousTabIndex === null) target.removeAttribute("tabindex");
        else target.setAttribute("tabindex", previousTabIndex);
      }
      if (closeHighlight === close) closeHighlight = null;
    };
    const observer = window.ResizeObserver ? new ResizeObserver(update) : null;
    observer?.observe(target);
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    window.addEventListener("pagehide", close);
    document.addEventListener("keydown", keyHandler, true);
    closeHighlight = close;
    update();
    next.focus();
  };

  const showMissingStep = step => {
    openDialog({
      eyebrow: "Adım kullanılamıyor",
      title: step.title,
      body: "Bu öğe şu anda sayfada bulunamadı. Yeniden deneyebilir veya sonraki adıma geçebilirsiniz.",
      buttons: [
        {
          label: "Sonraki Adım",
          className: "btn btn-secondary",
          action: async close => {
            close();
            await moveFrom(step, 1);
          },
        },
        {
          label: "Tekrar Dene",
          className: "btn btn-primary",
          primary: true,
          action: async close => {
            close();
            await renderStep(step);
          },
        },
      ],
    });
  };

  const renderStep = async step => {
    const route = step.route(state);
    if (window.location.pathname !== route) {
      window.location.assign(route);
      return;
    }
    step.prepare?.(state, step);
    const selector = typeof step.selector === "function" ? step.selector(state) : step.selector;
    const target = await waitForTarget(selector);
    if (!target) {
      step.cleanup?.(state, null);
      return showMissingStep(step);
    }
    target.scrollIntoView({behavior: reducedMotion ? "auto" : "smooth", block: "center", inline: "nearest"});
    window.setTimeout(() => createHighlight(target, step), reducedMotion ? 0 : 220);
  };

  const startTour = async () => {
    const first = firstAvailableStep();
    if (!first) return finishTour();
    await persistAndRender(first);
  };

  const resumeTour = async () => {
    const step = resolvePersistedStep();
    if (!step) return finishTour();
    if (state.current_step !== step.id || state.current_topic !== step.topic || state.status !== "in_progress") {
      await patchState({status: "in_progress", current_topic: step.topic, current_step: step.id});
    }
    await renderStep(step);
  };

  const showFatalError = async error => {
    if (error?.onboardingConflict) {
      closeSurfaces(null);
      if (state?.status === "in_progress") await resumeTour();
      return;
    }
    try {
      if (state?.enabled && state.status === "in_progress") await patchState({status: "paused"});
    } catch (_pauseError) {
      // Preserve the original error message.
    }
    closeSurfaces(null);
    openDialog({
      eyebrow: state?.status === "paused" ? "Tur duraklatıldı" : "Tur hatası",
      title: "Tur devam ettirilemedi",
      body: error.message || "Beklenmeyen bir hata oluştu. Turu üst menüden yeniden deneyebilirsiniz.",
      buttons: [{label: "Kapat", className: "btn btn-primary", primary: true, action: async close => close()}],
    });
  };

  const handleRestart = async () => {
    closeSurfaces(null);
    try {
      if (state.status === "paused" || state.status === "in_progress") {
        await resumeTour();
        return;
      }
      state = await fetchJson("/api/onboarding/restart", {
        method: "POST",
        body: JSON.stringify({
          tour_version: state.tour_version,
          expected_revision: state.revision,
        }),
      });
      syncRestartButtons();
      if (state.enabled) await startTour();
    } catch (error) {
      await showFatalError(error);
    }
  };

  restartButtons.forEach(button => button.addEventListener("click", handleRestart));

  document.addEventListener("DOMContentLoaded", async () => {
    try {
      await refreshState();
      if (!state.enabled) return;
      if (state.status === "in_progress") await resumeTour();
      else if (state.status === "not_started" && state.auto_offer) await startTour();
    } catch (error) {
      console.warn("Onboarding tour could not initialize", error);
    }
  });
})();
