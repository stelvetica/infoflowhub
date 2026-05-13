(() => {
  const STORAGE_KEY = "infoflowhub:read-links";
  const LATERHUB_COLLAPSED_KEY = "infoflowhub:laterhub-collapsed";

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
      };
    });
  }

  function setupUnreadToggle() {
    const slot = document.getElementById("entries-unread-slot");
    const panel = document.getElementById("entries-panel");
    if (!slot || !panel) return;
    if (slot.dataset.ready === "1") return;
    slot.dataset.ready = "1";
    let unreadOnly = true;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn ghost active-filter";
    button.textContent = "显示全部";
    button.onclick = () => {
      unreadOnly = !unreadOnly;
      const readLinks = loadReadLinks();
      panel.querySelectorAll("tbody tr").forEach((row) => {
        const anchor = row.querySelector(".read-track");
        if (!anchor) return;
        const href = anchor.getAttribute("href") || "";
        row.style.display = unreadOnly && readLinks[href] ? "none" : "";
      });
      button.textContent = unreadOnly ? "显示全部" : "未读";
      button.classList.toggle("active-filter", unreadOnly);
    };
    slot.innerHTML = "";
    slot.appendChild(button);
    button.click();
  }

  function clearModal() {
    const root = document.getElementById("modal-root");
    if (root) root.innerHTML = "";
  }

  function closeModal(event) {
    if (event.target.classList.contains("modal-backdrop")) clearModal();
  }

  function setupLaterhubToggle() {
    const toggle = document.querySelector("[data-toggle-laterhub]");
    const shell = document.querySelector(".split-shell");
    if (!toggle || !shell) return;
    const stored = window.localStorage.getItem(LATERHUB_COLLAPSED_KEY);
    if (stored === "1") {
      shell.classList.add("laterhub-collapsed");
      toggle.setAttribute("aria-pressed", "true");
      const knob = toggle.querySelector(".split-toggle-knob");
      if (knob) knob.textContent = "<<";
    }
    toggle.addEventListener("click", () => {
      const collapsed = shell.classList.toggle("laterhub-collapsed");
      window.localStorage.setItem(LATERHUB_COLLAPSED_KEY, collapsed ? "1" : "0");
      toggle.setAttribute("aria-pressed", collapsed ? "true" : "false");
      const knob = toggle.querySelector(".split-toggle-knob");
      if (knob) knob.textContent = collapsed ? "<<" : ">>";
    });
  }

  function init() {
    applyReadState();
    setupUnreadToggle();
    setupLaterhubToggle();
  }

  document.body.addEventListener("htmx:beforeRequest", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    const button = form.querySelector('button[type="submit"]');
    if (!(button instanceof HTMLButtonElement)) return;
    if (!button.dataset.originalText) {
      button.dataset.originalText = button.textContent || "";
    }
    if (button.dataset.originalText.includes("立即抓取")) {
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
