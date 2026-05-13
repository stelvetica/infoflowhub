(() => {
  const STORAGE_KEY = "infoflowhub:read-links";
  const LATERHUB_WIDTH_KEY = "infoflowhub:laterhub-width";
  const unreadFilter = {
    unreadOnly: true,
    button: null,
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

  function applyReadState() {
    const readLinks = loadReadLinks();
    document.querySelectorAll(".read-track").forEach((anchor) => {
      const href = anchor.getAttribute("href") || "";
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
        renderUnreadToggle();
      };
    });
  }

  function renderUnreadToggle() {
    const panel = document.getElementById("entries-panel");
    if (!panel) return;
    const { button, unreadOnly } = unreadFilter;
    panel.querySelectorAll("tbody tr").forEach((row) => {
      const anchor = row.querySelector(".read-track");
      if (!anchor) return;
      const href = anchor.getAttribute("href") || "";
      row.style.display = unreadOnly && loadReadLinks()[href] ? "none" : "";
    });
    if (button) {
      button.textContent = unreadOnly ? "默认未读" : "显示全部";
      button.classList.toggle("active-filter", unreadOnly);
    }
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
      };
      unreadFilter.button = button;
    }
    if (slot.firstChild !== button) {
      slot.innerHTML = "";
      slot.appendChild(button);
    }
    renderUnreadToggle();
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

  function init() {
    applyReadState();
    setupUnreadToggle();
    setupLaterhubResizer();
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
