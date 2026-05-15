(() => {
  const STORAGE_KEY = "infoflowhub:read-links:v2";
  const LATERHUB_WIDTH_KEY = "infoflowhub:laterhub-width";
  const unreadFilter = {
    unreadOnly: true,
    button: null,
  };
  const wechatLogin = {
    timer: null,
  };

  function loadReadLinks() {
    try {
      return JSON.parse(window.localStorage.getItem(STORAGE_KEY) || "{}");
    } catch {
      return {};
    }
  }

  function saveReadLinks(value) {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
  }

  function readLinkKey(value) {
    const text = String(value || "").trim();
    if (!text) return "";
    const bytes = new TextEncoder().encode(text);
    let binary = "";
    for (const byte of bytes) {
      binary += String.fromCharCode(byte);
    }
    return window.btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "").slice(0, 24);
  }

  function getReadKeys() {
    const readLinks = loadReadLinks();
    return Object.keys(readLinks)
      .filter((href) => readLinks[href])
      .map((href) => readLinkKey(href))
      .filter(Boolean);
  }

  function getEntriesFragment() {
    return document.querySelector("[data-entries-fragment]");
  }

  function syncEntriesUnreadFields() {
    const form = document.querySelector(".entries-header-form");
    if (!form) return;

    let unreadInput = form.querySelector('input[name="entries_unread_only"]');
    if (!unreadInput) {
      unreadInput = document.createElement("input");
      unreadInput.type = "hidden";
      unreadInput.name = "entries_unread_only";
      form.appendChild(unreadInput);
    }
    unreadInput.value = unreadFilter.unreadOnly ? "1" : "";

    let readKeysInput = form.querySelector('input[name="entries_read_keys"]');
    if (!readKeysInput) {
      readKeysInput = document.createElement("input");
      readKeysInput.type = "hidden";
      readKeysInput.name = "entries_read_keys";
      form.appendChild(readKeysInput);
    }
    readKeysInput.value = getReadKeys().join(",");
  }

  function refreshEntriesPanel(extraParams = {}) {
    if (!window.htmx) return;
    const form = document.querySelector(".entries-header-form");
    if (!form) return;

    syncEntriesUnreadFields();
    const params = new URLSearchParams();
    const formData = new FormData(form);
    formData.forEach((value, key) => {
      const text = String(value || "");
      if (text) params.set(key, text);
    });

    const meta = getEntriesFragment();
    if (meta?.dataset.page && !Object.prototype.hasOwnProperty.call(extraParams, "entries_page")) {
      params.set("entries_page", meta.dataset.page);
    }

    Object.entries(extraParams).forEach(([key, value]) => {
      const text = String(value || "");
      if (text) {
        params.set(key, text);
      } else {
        params.delete(key);
      }
    });

    window.htmx.ajax("GET", `/fragments/entries?${params.toString()}`, {
      target: "#entries-panel",
      swap: "innerHTML",
    });
  }

  function applyReadState() {
    const readLinks = loadReadLinks();
    let changed = false;
    document.querySelectorAll(".read-track").forEach((anchor) => {
      const href = anchor.getAttribute("href") || "";
      const row = anchor.closest("tr");
      const timeText = row?.querySelector(".cell-time")?.textContent?.trim() || "";
      if (timeText && timeText < "2026/05/01" && !readLinks[href]) {
        readLinks[href] = true;
        changed = true;
      }
      if (readLinks[href]) {
        anchor.classList.remove("cell-strong");
        anchor.classList.add("cell-read");
      } else {
        anchor.classList.remove("cell-read");
        anchor.classList.add("cell-strong");
      }
      anchor.onclick = () => {
        const next = loadReadLinks();
        next[href] = true;
        saveReadLinks(next);
        anchor.classList.remove("cell-strong");
        anchor.classList.add("cell-read");
        syncEntriesUnreadFields();
        if (unreadFilter.unreadOnly) {
          window.setTimeout(() => refreshEntriesPanel(), 0);
          return;
        }
        renderUnreadToggle();
      };
    });
    if (changed) {
      saveReadLinks(readLinks);
    }
  }

  function renderUnreadToggle() {
    const { button, unreadOnly } = unreadFilter;
    if (button) {
      button.textContent = unreadOnly ? "默认未读" : "显示全部";
      button.classList.toggle("active-filter", unreadOnly);
    }
    syncEntriesUnreadFields();
  }

  function setupUnreadToggle() {
    const slot = document.getElementById("entries-unread-slot");
    if (!slot) return;
    let button = unreadFilter.button;
    if (!button) {
      button = document.createElement("button");
      button.type = "button";
      button.className = "btn ghost active-filter";
      button.onclick = () => {
        unreadFilter.unreadOnly = !unreadFilter.unreadOnly;
        renderUnreadToggle();
        refreshEntriesPanel({ entries_page: "1" });
      };
      unreadFilter.button = button;
    }
    if (slot.firstChild !== button) {
      slot.innerHTML = "";
      slot.appendChild(button);
    }
    renderUnreadToggle();
    const meta = getEntriesFragment();
    if (meta && (meta.dataset.unreadOnly || "0") !== (unreadFilter.unreadOnly ? "1" : "0")) {
      refreshEntriesPanel({ entries_page: "1" });
    }
  }

  function clearModal() {
    const root = document.getElementById("modal-root");
    if (root) root.innerHTML = "";
  }

  function closeModal(event) {
    if (event.target.classList.contains("modal-backdrop")) clearModal();
  }

  function setupLaterhubResizer() {
    const resizer = document.querySelector("[data-split-resizer]");
    const shell = document.querySelector(".split-shell");
    if (!resizer || !shell) return;
    if (resizer.dataset.ready === "1") return;
    resizer.dataset.ready = "1";

    const clampWidth = (width) => {
      const shellWidth = shell.getBoundingClientRect().width;
      const minWidth = 320;
      const maxWidth = Math.max(minWidth, Math.min(720, Math.floor(shellWidth * 0.6)));
      return Math.min(maxWidth, Math.max(minWidth, Math.round(width)));
    };

    const applyWidth = (width, persist = true) => {
      if (window.innerWidth <= 1100) return;
      const nextWidth = clampWidth(width);
      shell.style.setProperty("--laterhub-width", `${nextWidth}px`);
      if (persist) {
        window.localStorage.setItem(LATERHUB_WIDTH_KEY, String(nextWidth));
      }
    };

    const restoreWidth = () => {
      if (window.innerWidth <= 1100) {
        shell.style.removeProperty("--laterhub-width");
        return;
      }
      const stored = Number(window.localStorage.getItem(LATERHUB_WIDTH_KEY) || "");
      if (Number.isFinite(stored) && stored > 0) {
        applyWidth(stored, false);
      } else {
        shell.style.removeProperty("--laterhub-width");
      }
    };

    let dragging = false;
    let activePointerId = null;

    const updateFromClientX = (clientX) => {
      const bounds = shell.getBoundingClientRect();
      applyWidth(bounds.right - clientX);
    };

    const stopDragging = (pointerId) => {
      if (!dragging) return;
      dragging = false;
      activePointerId = null;
      resizer.classList.remove("is-dragging");
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      if (pointerId !== undefined && resizer.hasPointerCapture(pointerId)) {
        resizer.releasePointerCapture(pointerId);
      }
    };

    resizer.addEventListener("pointerdown", (event) => {
      if (window.innerWidth <= 1100) return;
      event.preventDefault();
      dragging = true;
      activePointerId = event.pointerId;
      resizer.classList.add("is-dragging");
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      resizer.setPointerCapture(event.pointerId);
      updateFromClientX(event.clientX);
    });

    const handlePointerMove = (event) => {
      if (!dragging) return;
      if (activePointerId !== null && event.pointerId !== activePointerId) return;
      updateFromClientX(event.clientX);
    };

    const handlePointerUp = (event) => {
      if (activePointerId !== null && event.pointerId !== activePointerId) return;
      stopDragging(event.pointerId);
    };

    const handlePointerCancel = (event) => {
      if (activePointerId !== null && event.pointerId !== activePointerId) return;
      stopDragging(event.pointerId);
    };

    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", handlePointerUp);
    window.addEventListener("pointercancel", handlePointerCancel);

    window.addEventListener("resize", restoreWidth);
    restoreWidth();
  }

  async function setupWechatLogin() {
    const root = document.querySelector("[data-wechat-login]");
    if (!root || root.dataset.ready === "1") return;
    root.dataset.ready = "1";
    const qr = document.getElementById("wechat-login-qr");
    const status = document.getElementById("wechat-login-status");
    const refresh = document.getElementById("wechat-login-refresh");

    const setStatus = (text) => {
      if (status) status.textContent = text;
    };

    const stopPolling = () => {
      if (wechatLogin.timer) {
        window.clearTimeout(wechatLogin.timer);
        wechatLogin.timer = null;
      }
    };

    const completeLogin = async () => {
      setStatus("已扫码，正在完成登录...");
      const res = await fetch("/actions/wechat-login/complete", { method: "POST" });
      const data = await res.json();
      if (data.success) {
        setStatus(`登录成功：${data.data.nickname || "公众号"}`);
        window.setTimeout(() => {
          window.location.href = "/?view=settings";
        }, 1200);
        return;
      }
      setStatus(`登录失败：${data.error || "未知错误"}`);
    };

    const poll = async () => {
      try {
        const res = await fetch("/actions/wechat-login/scan");
        const data = await res.json();
        if (data.base_resp && data.base_resp.ret !== 0) {
          setStatus("状态检查失败，请刷新二维码后重试。");
          return;
        }
        const scanStatus = Number(data.status || 0);
        if (scanStatus === 1) {
          stopPolling();
          await completeLogin();
          return;
        }
        if (scanStatus === 4 || scanStatus === 6) {
          setStatus("已扫码，请在手机上确认登录。");
        } else if (scanStatus === 2) {
          stopPolling();
          setStatus("二维码已过期，请刷新二维码。");
          return;
        } else if (scanStatus === 3) {
          stopPolling();
          setStatus("扫码失败，请刷新二维码后重试。");
          return;
        } else {
          setStatus("请用微信扫码。");
        }
      } catch {
        setStatus("状态检查失败，请稍后重试。");
      }
      wechatLogin.timer = window.setTimeout(poll, 1800);
    };

    const loadQrcode = async () => {
      stopPolling();
      setStatus("正在准备二维码...");
      if (qr) qr.innerHTML = "正在加载二维码...";
      await fetch("/actions/wechat-login/start", { method: "POST" });
      const img = document.createElement("img");
      img.alt = "微信登录二维码";
      img.style.maxWidth = "220px";
      img.style.maxHeight = "220px";
      img.onload = () => {
        if (qr) {
          qr.innerHTML = "";
          qr.appendChild(img);
        }
        setStatus("请用微信扫码。");
        wechatLogin.timer = window.setTimeout(poll, 1800);
      };
      img.onerror = () => {
        setStatus("二维码加载失败，请刷新后重试。");
      };
      img.src = `/actions/wechat-login/qrcode?rnd=${Math.random()}`;
    };

    refresh?.addEventListener("click", loadQrcode);
    await loadQrcode();
  }

  function init() {
    applyReadState();
    setupUnreadToggle();
    setupLaterhubResizer();
    setupWechatLogin();
  }

  document.body.addEventListener("htmx:beforeRequest", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    const button = form.querySelector('button[type="submit"]');
    if (!(button instanceof HTMLButtonElement)) return;
    if (!button.dataset.originalText) {
      button.dataset.originalText = button.textContent || "";
    }
    if (button.dataset.originalText.includes("抓取")) {
      button.textContent = "抓取中...";
      button.disabled = true;
    }
  });

  document.body.addEventListener("htmx:afterRequest", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    const button = form.querySelector('button[type="submit"]');
    if (!(button instanceof HTMLButtonElement)) return;
    if (button.dataset.originalText) {
      button.textContent = button.dataset.originalText;
      button.disabled = false;
    }
  });

  window.infoflowhub = { clearModal, closeModal };
  document.addEventListener("DOMContentLoaded", init);
  document.body.addEventListener("htmx:afterSwap", init);
})();
