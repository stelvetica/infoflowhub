(() => {
  const STORAGE_KEY = "infoflowhub:read-links:v3";
  // 中缝拖拽:存比例而非像素,窗口缩放后比例依然成立,故无需 resize 监听。
  const SPLIT_RATIO_KEY = "infoflowhub:laterhub-ratio";
  const LEGACY_SPLIT_WIDTH_KEY = "infoflowhub:laterhub-width";
  const SPLIT_MAX_RATIO = 0.6;
  // 每个订阅面板(主订阅区/新闻卡片)独立的交互配置与未读态。
  const panels = [
    {
      kind: "entries",
      formSel: ".entries-header-form",
      panelSel: "#entries-panel",
      slotSel: "#entries-unread-slot",
      fragmentUrl: "/fragments/entries",
      prefix: "entries",
      unreadOnly: true,
      button: null,
      lastSyncSignature: "",
    },
    {
      kind: "news",
      formSel: ".news-header-form",
      panelSel: "#news-panel",
      slotSel: "#news-unread-slot",
      fragmentUrl: "/fragments/news",
      prefix: "news",
      unreadOnly: true,
      button: null,
      lastSyncSignature: "",
    },
  ];
  const wechatLogin = {
    timer: null,
  };
  const fetchNow = {
    timer: null,
    pollCount: 0,
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

  function setCookie(name, value, days = 180) {
    const expires = new Date(Date.now() + days * 24 * 60 * 60 * 1000).toUTCString();
    document.cookie = `${name}=${value}; expires=${expires}; path=/; SameSite=Lax`;
  }

  function readLinkKey(value) {
    const text = String(value || "").trim();
    if (!text) return "";
    function rotateLeft(num, cnt) {
      return (num << cnt) | (num >>> (32 - cnt));
    }
    const bytes = unescape(encodeURIComponent(text));
    const words = [];
    for (let i = 0; i < bytes.length; i += 1) {
      words[i >> 2] |= bytes.charCodeAt(i) << (24 - (i % 4) * 8);
    }
    words[bytes.length >> 2] |= 0x80 << (24 - (bytes.length % 4) * 8);
    words[(((bytes.length + 8) >> 6) + 1) * 16 - 1] = bytes.length * 8;

    let h0 = 0x67452301;
    let h1 = 0xefcdab89;
    let h2 = 0x98badcfe;
    let h3 = 0x10325476;
    let h4 = 0xc3d2e1f0;

    for (let i = 0; i < words.length; i += 16) {
      const w = new Array(80);
      for (let j = 0; j < 16; j += 1) w[j] = words[i + j] | 0;
      for (let j = 16; j < 80; j += 1) w[j] = rotateLeft(w[j - 3] ^ w[j - 8] ^ w[j - 14] ^ w[j - 16], 1);

      let a = h0;
      let b = h1;
      let c = h2;
      let d = h3;
      let e = h4;

      for (let j = 0; j < 80; j += 1) {
        let f = 0;
        let k = 0;
        if (j < 20) {
          f = (b & c) | (~b & d);
          k = 0x5a827999;
        } else if (j < 40) {
          f = b ^ c ^ d;
          k = 0x6ed9eba1;
        } else if (j < 60) {
          f = (b & c) | (b & d) | (c & d);
          k = 0x8f1bbcdc;
        } else {
          f = b ^ c ^ d;
          k = 0xca62c1d6;
        }
        const temp = (rotateLeft(a, 5) + f + e + k + w[j]) | 0;
        e = d;
        d = c;
        c = rotateLeft(b, 30);
        b = a;
        a = temp;
      }

      h0 = (h0 + a) | 0;
      h1 = (h1 + b) | 0;
      h2 = (h2 + c) | 0;
      h3 = (h3 + d) | 0;
      h4 = (h4 + e) | 0;
    }

    return [h0, h1, h2, h3, h4]
      .map((n) => (n >>> 0).toString(16).padStart(8, "0"))
      .join("");
  }

  function getReadKeys() {
    const readLinks = loadReadLinks();
    return Object.keys(readLinks)
      .filter((href) => readLinks[href])
      .map((href) => {
        if (href.startsWith("key:")) return href.slice(4);
        return readLinkKey(href);
      })
      .filter(Boolean);
  }

  function getPanelFragment(panel) {
    return document.querySelector(`[data-${panel.kind}-fragment]`);
  }

  function syncPanelFields(panel) {
    const form = document.querySelector(panel.formSel);
    if (!form) return;

    let unreadInput = form.querySelector(`input[name="${panel.prefix}_unread_only"]`);
    if (!unreadInput) {
      unreadInput = document.createElement("input");
      unreadInput.type = "hidden";
      unreadInput.name = `${panel.prefix}_unread_only`;
      form.appendChild(unreadInput);
    }
    unreadInput.value = panel.unreadOnly ? "1" : "";

    let readKeysInput = form.querySelector(`input[name="${panel.prefix}_read_keys"]`);
    if (!readKeysInput) {
      readKeysInput = document.createElement("input");
      readKeysInput.type = "hidden";
      readKeysInput.name = `${panel.prefix}_read_keys`;
      form.appendChild(readKeysInput);
    }
    readKeysInput.value = getReadKeys().join(",");
    const cookieParams = new URLSearchParams();
    const pageValue = getPanelFragment(panel)?.dataset.page || "1";
    cookieParams.set(`${panel.prefix}_unread_only`, unreadInput.value || "");
    cookieParams.set(`${panel.prefix}_read_keys`, readKeysInput.value || "");
    // 关键字统一来自顶栏全局搜索框(设置页无全局框时回退表单字段)
    const globalInput = document.getElementById("global-search");
    const keyword = globalInput?.value ?? form.querySelector(`input[name="${panel.prefix}_q"]`)?.value ?? "";
    cookieParams.set(`${panel.prefix}_q`, keyword);
    cookieParams.set(`${panel.prefix}_sort`, form.querySelector(`input[name="${panel.prefix}_sort"]`)?.value || "sort_time");
    cookieParams.set(`${panel.prefix}_dir`, form.querySelector(`input[name="${panel.prefix}_dir"]`)?.value || "desc");
    cookieParams.set(`${panel.prefix}_page`, pageValue);
    setCookie(`ifh_${panel.kind}_state_v1`, cookieParams.toString());
  }

  function buildPanelQuery(panel, extraParams = {}) {
    const form = document.querySelector(panel.formSel);
    if (!form) return null;

    syncPanelFields(panel);
    const params = new URLSearchParams();
    const formData = new FormData(form);
    formData.forEach((value, key) => {
      const text = String(value || "");
      if (text) params.set(key, text);
    });

    const meta = getPanelFragment(panel);
    if (meta?.dataset.page && !Object.prototype.hasOwnProperty.call(extraParams, `${panel.prefix}_page`)) {
      params.set(`${panel.prefix}_page`, meta.dataset.page);
    }

    Object.entries(extraParams).forEach(([key, value]) => {
      const text = String(value || "");
      if (text) {
        params.set(key, text);
      } else {
        params.delete(key);
      }
    });

    return params;
  }

  function refreshPanel(panel, extraParams = {}) {
    if (!window.htmx) return;
    const params = buildPanelQuery(panel, extraParams);
    if (!params) return;

    window.htmx.ajax("GET", `${panel.fragmentUrl}?${params.toString()}`, {
      target: panel.panelSel,
      swap: "innerHTML",
    });
  }

  function findPanel(kind) {
    return panels.find((panel) => panel.kind === kind) || null;
  }

  // 表格续载:先把表格区填满(不留空白),滚到接近底部再追加下一页,故不再需要「下一页」按钮。
  // 已加载页数按片段根元素记(WeakMap):HTMX 换掉片段根即自动归零,无需额外重置逻辑。
  const AUTO_LOAD_TRIGGER_PX = 160;
  const AUTO_LOAD_FILL_ROUNDS = 5;
  const autoLoadPages = new WeakMap();
  const autoLoadPanels = [
    {
      key: "news",
      metaSel: "[data-news-fragment]",
      wrapSel: "#news-panel .table-wrap",
      tbodySel: "#news-panel tbody",
      countSel: "#news-panel .pagination .subtle",
      loading: false,
      nextUrl(page) {
        const panel = findPanel("news");
        const params = panel ? buildPanelQuery(panel, { news_page: String(page) }) : null;
        return params ? `${panel.fragmentUrl}?${params.toString()}` : "";
      },
    },
    {
      key: "entries",
      metaSel: "[data-entries-fragment]",
      wrapSel: "#entries-panel .table-wrap",
      tbodySel: "#entries-panel tbody",
      countSel: "#entries-panel .pagination .subtle",
      loading: false,
      nextUrl(page) {
        const panel = findPanel("entries");
        const params = panel ? buildPanelQuery(panel, { entries_page: String(page) }) : null;
        return params ? `${panel.fragmentUrl}?${params.toString()}` : "";
      },
    },
    {
      key: "laterhub",
      metaSel: "[data-laterhub-fragment]",
      wrapSel: ".pane-right-stack .table-wrap",
      tbodySel: ".pane-right-stack tbody",
      countSel: ".pane-right-stack .pagination .subtle",
      loading: false,
      nextUrl(page) {
        const params = new URLSearchParams(getLaterhubQueryString());
        params.set("laterhub_page", String(page));
        return `/fragments/laterhub?${params.toString()}`;
      },
    },
  ];

  function readAutoLoadState(descriptor) {
    const meta = document.querySelector(descriptor.metaSel);
    const wrap = document.querySelector(descriptor.wrapSel);
    const tbody = document.querySelector(descriptor.tbodySel);
    if (!meta || !wrap || !tbody) return null;
    return {
      meta,
      wrap,
      tbody,
      basePage: Number(meta.dataset.page || 1) || 1,
      totalPages: Number(meta.dataset.totalPages || 1) || 1,
      total: Number(meta.dataset.total || 0) || 0,
    };
  }

  function loadedRowCount(tbody) {
    return [...tbody.querySelectorAll("tr")].filter((row) => !row.querySelector(".empty")).length;
  }

  function countText(loaded, total) {
    return `已加载 ${loaded} / 共 ${total} 条`;
  }

  function updateAutoLoadCount(descriptor, state) {
    const count = document.querySelector(descriptor.countSel);
    const tbody = document.querySelector(descriptor.tbodySel);
    if (!count || !tbody) return;
    count.textContent = countText(loadedRowCount(tbody), state.total);
  }

  // 「完成」就地生效:只换那一行(当前筛选不再匹配则移除该行),不重绘整个面板——
  // 面板整块重绘会丢掉已续载的行与滚动位置。总数取自响应里的片段根 data-total。
  function setupLaterhubFinishForms() {
    document.querySelectorAll(".laterhub-action-cell form[data-toggle-url]").forEach((form) => {
      if (form.dataset.finishReady === "1") return;
      form.dataset.finishReady = "1";
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const button = form.querySelector("button");
        if (button) button.disabled = true;
        try {
          const response = await fetch(form.dataset.toggleUrl, {
            method: "POST",
            body: new FormData(form),
            credentials: "same-origin",
          });
          if (!response.ok) throw new Error(String(response.status));
          applyLaterhubToggle(await response.text(), form.dataset.linkId || form.closest("tr")?.dataset.linkId);
        } catch {
          if (button) button.disabled = false; // 失败保持原样,可再点
        }
      });
    });
  }

  function applyLaterhubToggle(html, linkId) {
    const row = document.querySelector(`.pane-right-stack tr[data-link-id="${linkId}"]`);
    if (!row) return;
    const parsed = new DOMParser().parseFromString(html, "text/html");
    const fresh = parsed.querySelector(`tr[data-link-id="${linkId}"]`);
    if (fresh) {
      row.replaceWith(document.importNode(fresh, true));
    } else {
      // 当前筛选(如「未处理」)已不含该条目
      row.remove();
    }
    const total = parsed.querySelector("[data-laterhub-fragment]")?.dataset.total;
    const meta = document.querySelector("[data-laterhub-fragment]");
    if (total && meta) meta.dataset.total = total;
    const count = document.querySelector(".pane-right-stack .pagination .subtle");
    const tbody = document.querySelector(".pane-right-stack tbody");
    if (count && tbody && total) count.textContent = countText(loadedRowCount(tbody), total);
    setupLaterhubFinishForms();
    setupLaterhubOpenedTracking();
    applyReadState();
    const descriptor = autoLoadPanels.find((item) => item.key === "laterhub");
    if (descriptor) fillAutoLoadPanel(descriptor); // 移除一行后若区格变空,顺手补上
  }

  // 表格滚动条:静止时不显示,滚动时才浮现(Windows Chrome 默认那条带箭头的粗条太抢眼)
  const SCROLLBAR_IDLE_MS = 1000;

  function setupScrollbarReveal() {
    document.querySelectorAll(".table-wrap").forEach((wrap) => {
      if (wrap.dataset.scrollbarReady === "1") return;
      wrap.dataset.scrollbarReady = "1";
      let timer = null;
      wrap.addEventListener("scroll", () => {
        wrap.classList.add("is-scrolling");
        window.clearTimeout(timer);
        timer = window.setTimeout(() => wrap.classList.remove("is-scrolling"), SCROLLBAR_IDLE_MS);
      });
    });
  }

  async function appendNextPage(descriptor) {
    if (descriptor.loading) return false;
    const state = readAutoLoadState(descriptor);
    if (!state) return false;
    const loaded = autoLoadPages.get(state.meta) ?? state.basePage;
    if (loaded >= state.totalPages) return false;
    const url = descriptor.nextUrl(loaded + 1);
    if (!url) return false;

    descriptor.loading = true;
    try {
      const response = await fetch(url, { credentials: "same-origin" });
      if (!response.ok) throw new Error(String(response.status));
      const html = await response.text();
      const source = new DOMParser().parseFromString(html, "text/html").querySelector("tbody");
      const target = document.querySelector(descriptor.tbodySel);
      if (!source || !target) return false;
      const existingRows = target.children.length;
      target.insertAdjacentHTML("beforeend", source.innerHTML);
      // 追加的行没经过 HTMX 交换,需手动 process 才能让 hx-post 等属性生效(否则「完成」按钮是死的)
      const appendedRows = [...target.children].slice(existingRows);
      if (window.htmx?.process) appendedRows.forEach((row) => window.htmx.process(row));
      autoLoadPages.set(state.meta, loaded + 1);
      updateAutoLoadCount(descriptor, state);
      applyReadState();
      setupLaterhubOpenedTracking();
      setupLaterhubFinishForms();
      return true;
    } catch {
      // 失败不打断浏览:计数行提示,继续滚动会再试
      const count = document.querySelector(descriptor.countSel);
      if (count) count.textContent = "加载失败，继续滚动重试";
      return false;
    } finally {
      descriptor.loading = false;
    }
  }

  async function fillAutoLoadPanel(descriptor) {
    for (let round = 0; round < AUTO_LOAD_FILL_ROUNDS; round += 1) {
      const state = readAutoLoadState(descriptor);
      if (!state) return;
      // 表格区已出现滚动即视为填满;取完或空结果由 appendNextPage 自行停下
      if (state.wrap.scrollHeight > state.wrap.clientHeight + 1) return;
      if (!(await appendNextPage(descriptor))) return;
    }
  }

  function bindAutoLoadScroll(descriptor) {
    const state = readAutoLoadState(descriptor);
    if (!state || state.wrap.dataset.autoLoadBound === "1") return;
    state.wrap.dataset.autoLoadBound = "1";
    const wrap = state.wrap;
    wrap.addEventListener("scroll", () => {
      if (wrap.scrollTop + wrap.clientHeight < wrap.scrollHeight - AUTO_LOAD_TRIGGER_PX) return;
      appendNextPage(descriptor).then((appended) => {
        if (appended) fillAutoLoadPanel(descriptor);
      });
    });
  }

  function setEntryReadState(anchor, isRead) {
    anchor.classList.remove("cell-read", "cell-unread", "cell-strong");
    if (isRead) {
      anchor.classList.add("cell-read");
      return;
    }
    anchor.classList.add("cell-unread");
  }

  function syncReadStateToServer(readKey) {
    const value = String(readKey || "").trim();
    if (!value) return Promise.resolve(false);
    const formData = new FormData();
    formData.set("read_key", value);
    return fetch("/actions/entries/mark-read", {
      method: "POST",
      body: formData,
      credentials: "same-origin",
    })
      .then((res) => res.ok ? res.json() : { success: false })
      .then((data) => Boolean(data?.success))
      .catch(() => false);
  }

  function getEntryStorageState(anchor, readLinks) {
    const href = anchor.getAttribute("href") || "";
    const explicitReadKey = anchor.dataset.readKey || "";
    const explicitStorageKey = explicitReadKey ? `key:${explicitReadKey}` : "";
    const legacyStorageKeys = [];
    if (href) {
      legacyStorageKeys.push(href);
      legacyStorageKeys.push(`key:${readLinkKey(href)}`);
    }
    const isRead = Boolean(
      (explicitStorageKey && readLinks[explicitStorageKey]) ||
      legacyStorageKeys.some((key) => key && readLinks[key]),
    );
    return { href, explicitReadKey, explicitStorageKey, legacyStorageKeys, isRead };
  }

  function applyReadState() {
    const readLinks = loadReadLinks();
    let changed = false;
    document.querySelectorAll(".read-track").forEach((anchor) => {
      const {
        href,
        explicitReadKey,
        explicitStorageKey,
        legacyStorageKeys,
        isRead,
      } = getEntryStorageState(anchor, readLinks);
      if (!href) {
        setEntryReadState(anchor, true);
        return;
      }
      const row = anchor.closest("tr");
      const timeText = row?.querySelector(".cell-time")?.textContent?.trim() || "";
      if (timeText && timeText < "2026/05/01" && !isRead) {
        if (explicitStorageKey) {
          readLinks[explicitStorageKey] = true;
        } else {
          readLinks[href] = true;
        }
        changed = true;
      }
      if (isRead && explicitStorageKey && !readLinks[explicitStorageKey]) {
        readLinks[explicitStorageKey] = true;
        changed = true;
        syncReadStateToServer(explicitReadKey).catch(() => false);
      }
      legacyStorageKeys.forEach((key) => {
        if (key && readLinks[key] && explicitStorageKey) {
          delete readLinks[key];
          changed = true;
        }
      });
      const currentReadState = Boolean(explicitStorageKey ? readLinks[explicitStorageKey] : readLinks[href]);
      setEntryReadState(anchor, currentReadState);
      const kindNode = anchor.closest("[data-panel-kind]");
      const panel = panels.find((p) => p.kind === (kindNode?.dataset?.panelKind || "")) || panels[0];
      anchor.onclick = () => {
        const next = loadReadLinks();
        if (explicitStorageKey) {
          next[explicitStorageKey] = true;
          legacyStorageKeys.forEach((key) => {
            if (key) delete next[key];
          });
        } else {
          next[href] = true;
        }
        saveReadLinks(next);
        setEntryReadState(anchor, true);
        syncPanelFields(panel);
        syncReadStateToServer(explicitReadKey || readLinkKey(href)).then(() => {
          if (panel.unreadOnly) {
            refreshPanel(panel);
          }
        });
        if (panel.unreadOnly) {
          return;
        }
        renderUnreadToggle(panel);
      };
    });
    if (changed) {
      saveReadLinks(readLinks);
    }
  }

  function renderUnreadToggle(panel) {
    if (!panel) return;
    const { button, unreadOnly } = panel;
    if (button) {
      button.textContent = unreadOnly ? "默认未读" : "显示全部";
      button.classList.toggle("active-filter", unreadOnly);
    }
    syncPanelFields(panel);
  }

  function setupUnreadToggle(panel) {
    const slot = document.getElementById(panel.slotSel.replace("#", ""));
    if (!slot) return;
    const meta = getPanelFragment(panel);
    if (meta) {
      panel.unreadOnly = (meta.dataset.unreadOnly || "0") === "1";
    }
    let button = panel.button;
    if (!button) {
      button = document.createElement("button");
      button.type = "button";
      button.className = "btn ghost active-filter";
      button.onclick = () => {
        panel.unreadOnly = !panel.unreadOnly;
        renderUnreadToggle(panel);
        refreshPanel(panel, { [`${panel.prefix}_page`]: "1" });
      };
      panel.button = button;
    }
    if (slot.firstChild !== button) {
      slot.innerHTML = "";
      slot.appendChild(button);
    }
    renderUnreadToggle(panel);
    const currentReadKeys = getReadKeys().join(",");
    if (!meta) return;
    const currentPage = meta.dataset.page || "1";
    const renderedSignature = `${currentPage}|${meta.dataset.unreadOnly || "0"}|${meta.dataset.readKeys || ""}`;
    const targetSignature = `${currentPage}|${panel.unreadOnly ? "1" : "0"}|${currentReadKeys}`;
    if (renderedSignature !== targetSignature && panel.lastSyncSignature !== targetSignature) {
      panel.lastSyncSignature = targetSignature;
      refreshPanel(panel, { [`${panel.prefix}_page`]: currentPage });
      return;
    }
    panel.lastSyncSignature = targetSignature;
  }

  function setupLaterhubTagsToggle() {
    const btn = document.querySelector(".laterhub-tags-toggle-btn");
    const chips = document.getElementById("laterhub-chips");
    if (!btn || !chips || btn.dataset.ready === "1") return;
    btn.dataset.ready = "1";
    btn.addEventListener("click", () => {
      const willOpen = chips.hidden;
      chips.hidden = !willOpen;
      btn.setAttribute("aria-expanded", String(willOpen));
      btn.classList.toggle("open", willOpen);
    });
  }

  // 顶栏全局搜索:回车把关键字同时下发到新闻/订阅/稍后处理,清空回车即取消筛选
  function setupGlobalSearch() {
    const form = document.getElementById("global-search-form");
    const input = document.getElementById("global-search");
    if (!form || !input || form.dataset.ready === "1") return;
    form.dataset.ready = "1";
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const term = input.value.trim();
      input.value = term;
      panels.forEach((panel) => {
        refreshPanel(panel, { [`${panel.prefix}_q`]: term, [`${panel.prefix}_page`]: "1" });
      });
      const laterhubPanel = document.getElementById("laterhub-panel");
      if (laterhubPanel) {
        laterhubPanel.querySelectorAll('input[name="laterhub_q"]').forEach((el) => {
          el.value = term;
        });
        laterhubPanel.querySelector(".laterhub-form-stack")?.requestSubmit();
      }
    });
  }

  // 中缝拖拽:拖动改左右栏比例,松手即存(存比例,窗口缩放后依然成立)
  function setupSplitResizer() {
    const resizer = document.querySelector("[data-split-resizer]");
    const shell = document.querySelector(".split-shell");
    if (!resizer || !shell || resizer.dataset.ready === "1") return;
    resizer.dataset.ready = "1";
    // 旧的像素宽度键已废弃,留着会让人以为它仍在生效
    window.localStorage.removeItem(LEGACY_SPLIT_WIDTH_KEY);

    // 下限取 CSS 里的 --laterhub-min,不重复写死像素
    const minSide = parseFloat(getComputedStyle(shell).getPropertyValue("--laterhub-min")) || 320;
    const clampRatio = (ratio) => Math.min(SPLIT_MAX_RATIO, Math.max(minSide / shell.clientWidth, ratio));

    const applyRatio = (ratio, persist = false) => {
      const next = clampRatio(ratio);
      shell.style.setProperty("--laterhub-width", `${(next * 100).toFixed(2)}%`);
      resizer.setAttribute("aria-valuenow", String(Math.round(next * 100)));
      if (persist) window.localStorage.setItem(SPLIT_RATIO_KEY, String(next));
    };

    const ratioFromClientX = (clientX) => {
      const bounds = shell.getBoundingClientRect();
      return (bounds.right - clientX) / bounds.width;
    };

    let activePointerId = null;

    const stopDragging = (pointerId) => {
      if (activePointerId === null) return;
      activePointerId = null;
      resizer.classList.remove("is-dragging");
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      if (pointerId !== undefined && resizer.hasPointerCapture(pointerId)) {
        resizer.releasePointerCapture(pointerId);
      }
    };

    resizer.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      activePointerId = event.pointerId;
      resizer.classList.add("is-dragging");
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      resizer.setPointerCapture(event.pointerId);
      applyRatio(ratioFromClientX(event.clientX));
    });

    window.addEventListener("pointermove", (event) => {
      if (activePointerId === null || event.pointerId !== activePointerId) return;
      applyRatio(ratioFromClientX(event.clientX));
    });

    window.addEventListener("pointerup", (event) => {
      if (activePointerId === null || event.pointerId !== activePointerId) return;
      applyRatio(ratioFromClientX(event.clientX), true);
      stopDragging(event.pointerId);
    });

    window.addEventListener("pointercancel", (event) => {
      if (activePointerId === null || event.pointerId !== activePointerId) return;
      stopDragging(event.pointerId);
    });

    // 键盘替代:聚焦中缝后用左右方向键微调(±2%),同样即调即存
    resizer.addEventListener("keydown", (event) => {
      const step = event.key === "ArrowLeft" ? 0.02 : event.key === "ArrowRight" ? -0.02 : 0;
      if (!step) return;
      event.preventDefault();
      const side = shell.querySelector(".split-pane-side");
      const current = side ? side.getBoundingClientRect().width / shell.clientWidth : 0;
      applyRatio(current + step, true);
    });

    const stored = Number(window.localStorage.getItem(SPLIT_RATIO_KEY) || "");
    if (Number.isFinite(stored) && stored > 0) applyRatio(stored);
  }

  function clearModal() {
    const root = document.getElementById("modal-root");
    if (root) root.innerHTML = "";
  }

  function setLaterhubOpenState(anchor, isOpened) {
    anchor.classList.remove("cell-read", "cell-unread", "cell-strong");
    if (isOpened) {
      anchor.classList.add("cell-read");
      return;
    }
    anchor.classList.add("cell-unread");
  }

  function getLaterhubQueryString() {
    const panel = document.getElementById("laterhub-panel");
    if (!panel) return window.location.search || "";
    const form = panel.querySelector(".laterhub-form-stack");
    if (!(form instanceof HTMLFormElement)) return window.location.search || "";
    const params = new URLSearchParams(window.location.search || "");
    const formData = new FormData(form);
    formData.forEach((value, key) => {
      params.delete(key);
      const text = String(value || "");
      if (text) params.set(key, text);
    });
    return params.toString() ? `?${params.toString()}` : "";
  }

  function setupLaterhubOpenedTracking() {
    document.querySelectorAll(".laterhub-read-track").forEach((anchor) => {
      const href = anchor.getAttribute("href") || "";
      const linkId = anchor.dataset.linkId || "";
      if (!href || !linkId) return;
      setLaterhubOpenState(anchor, anchor.classList.contains("cell-read"));
      anchor.onclick = () => {
        setLaterhubOpenState(anchor, true);
        fetch(`/actions/laterhub/${linkId}/mark-opened${getLaterhubQueryString()}`, {
          method: "POST",
          credentials: "same-origin",
        }).catch(() => {});
      };
    });
  }

  function closeModal(event) {
    if (event.target.classList.contains("modal-backdrop")) clearModal();
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

    const renderQrcode = (scanUrl) => {
      if (!qr) return;
      qr.innerHTML = "";
      if (!scanUrl) {
        qr.innerHTML = "正在加载二维码…";
        return;
      }
      const img = document.createElement("img");
      img.alt = "微信读书登录二维码";
      img.width = 220;
      img.height = 220;
      img.style.maxWidth = "220px";
      img.style.maxHeight = "220px";
      img.src = `https://api.qrserver.com/v1/create-qr-code/?size=220x220&data=${encodeURIComponent(scanUrl)}`;
      qr.appendChild(img);
    };

    const handleSuccess = (data) => {
      stopPolling();
      const nickname = data.nickname || data.account_info?.nickname || "微信读书";
      setStatus(`✅ 登录成功：${nickname}`);
      const nextUrl = root.dataset.nextUrl || "/?view=settings";
      let remaining = 3;
      const tick = () => {
        if (remaining > 0) {
          setStatus(`✅ 登录成功：${nickname}（${remaining} 秒后自动返回）`);
          remaining -= 1;
          wechatLogin.timer = window.setTimeout(tick, 1000);
          return;
        }
        window.location.href = nextUrl;
      };
      tick();
    };

    const poll = async () => {
      try {
        const res = await fetch("/actions/weread-login/status");
        const data = await res.json();
        if (data.login_state && data.login_state.status === "failed") {
          stopPolling();
          setStatus(`登录失败：${data.login_state.message || "未知错误"}，请点击「刷新二维码」重试。`);
          return;
        }
        if (data.logged_in && !data.login_state.qrcode) {
          handleSuccess(data);
          return;
        }
        if (data.login_state && data.login_state.status === "scanning" && data.login_state.qrcode) {
          renderQrcode(data.login_state.qrcode);
          setStatus("请用微信读书 App 扫码，并在手机上确认登录。");
        } else {
          setStatus(data.message || "请用微信读书 App 扫码。");
        }
      } catch {
        setStatus("状态检查失败，请确认外部工具（127.0.0.1:5300）已启动后重试。");
      }
      wechatLogin.timer = window.setTimeout(poll, 2000);
    };

    const loadQrcode = async () => {
      stopPolling();
      setStatus("正在准备二维码…");
      if (qr) qr.innerHTML = "正在加载二维码…";
      try {
        await fetch("/actions/weread-login/start", { method: "POST" });
        const res = await fetch("/actions/weread-login/status");
        const data = await res.json();
        if (data.login_state && data.login_state.status === "scanning" && data.login_state.qrcode) {
          renderQrcode(data.login_state.qrcode);
          setStatus("请用微信读书 App 扫码，并在手机上确认登录。");
        } else if (data.logged_in) {
          handleSuccess(data);
          return;
        } else if (data.login_state && data.login_state.status === "failed") {
          setStatus(`登录失败：${data.login_state.message || "未知错误"}。`);
          return;
        } else {
          setStatus(data.message || "请用微信读书 App 扫码。");
        }
        wechatLogin.timer = window.setTimeout(poll, 2000);
      } catch {
        setStatus("启动登录失败，请确认外部工具（127.0.0.1:5300）已启动。");
      }
    };

    refresh?.addEventListener("click", loadQrcode);
    await loadQrcode();
  }

  function stopFetchPolling() {
    if (fetchNow.timer) {
      window.clearTimeout(fetchNow.timer);
      fetchNow.timer = null;
    }
  }

  async function pollFetchStatus() {
    try {
      const res = await fetch("/fragments/fetch-status");
      const data = await res.json();
      if (data.running || data.fetch_state === "running") {
        // 仍在抓取:最多轮询 5 分钟,继续等待
        fetchNow.pollCount += 1;
        if (fetchNow.pollCount <= 150) {
          fetchNow.timer = window.setTimeout(pollFetchStatus, 2000);
        } else {
          // 轮询超时放弃:复位按钮,避免转圈永不结束
          stopFetchPolling();
          finishFetchButtons();
        }
        return;
      }
    } catch {
      stopFetchPolling();
      finishFetchButtons();
      return;
    }
    // 抓取完成:复位按钮,刷新运行状态面板与各订阅面板
    stopFetchPolling();
    finishFetchButtons();
    const statusPanel = document.querySelector("#runtime-status-panel");
    const params = window.location.search || "";
    if (statusPanel) {
      window.htmx?.ajax("GET", `/fragments/runtime-status${params}`, {
        target: "#runtime-status-panel",
        swap: "innerHTML",
      });
    }
    panels.forEach((panel) => {
      refreshPanel(panel);
      renderUnreadToggle(panel);
    });
  }

  function startFetchPolling() {
    stopFetchPolling();
    fetchNow.pollCount = 0;
    fetchNow.timer = window.setTimeout(pollFetchStatus, 1500);
  }

  function init() {
    applyReadState();
    panels.forEach((panel) => setupUnreadToggle(panel));
    setupGlobalSearch();
    setupLaterhubOpenedTracking();
    setupLaterhubTagsToggle();
    setupSplitResizer();
    setupWechatLogin();
    setupLaterhubFinishForms();
    setupScrollbarReveal();
    autoLoadPanels.forEach((descriptor) => {
      bindAutoLoadScroll(descriptor);
      fillAutoLoadPanel(descriptor);
    });
  }

  // 抓取按钮 loading 态:点击即转圈。订阅抓取的 POST 秒回、后台才真正执行,
  // 转圈需持续到轮询确认完成;稍后处理为同步请求,响应返回即复位。
  function fetchLoadingButton(form) {
    if (!(form instanceof HTMLFormElement)) return null;
    const button = form.querySelector('button[type="submit"]');
    if (!(button instanceof HTMLButtonElement)) return null;
    if (!button.dataset.originalText) {
      button.dataset.originalText = button.textContent || "";
    }
    if (!button.dataset.originalText.includes("抓取")) return null;
    return button;
  }

  function setButtonLoading(button, loading) {
    if (!button) return;
    button.classList.toggle("is-loading", loading);
    button.textContent = loading ? "抓取中" : button.dataset.originalText || "";
    button.disabled = loading;
  }

  document.body.addEventListener("htmx:beforeRequest", (event) => {
    const button = fetchLoadingButton(event.target);
    if (button) setButtonLoading(button, true);
  });

  document.body.addEventListener("htmx:afterRequest", (event) => {
    const button = fetchLoadingButton(event.target);
    if (!button) return;
    // 稍后处理(/actions/laterhub/fetch-now)同步执行,响应返回即完成,直接复位;
    // 订阅抓取的 POST 秒回、后台才真正执行,保持转圈,由轮询结束后统一复位。
    const isLaterhub = !!button.closest('form[hx-post*="/actions/laterhub/fetch-now"]');
    if (isLaterhub) {
      setButtonLoading(button, false);
    } else {
      startFetchPolling();
    }
  });

  function finishFetchButtons() {
    document.querySelectorAll('.btn.is-loading[data-original-text]').forEach((button) => {
      setButtonLoading(button, false);
    });
  }

  // 模态框:Esc 关闭,打开后焦点移入首个输入框
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") clearModal();
  });
  document.body.addEventListener("htmx:afterSwap", () => {
    const field = document.querySelector(".modal-card input:not([type='hidden']), .modal-card select, .modal-card textarea");
    if (field instanceof HTMLElement) field.focus();
  });

  window.infoflowhub = { clearModal, closeModal };
  document.addEventListener("DOMContentLoaded", init);
  document.body.addEventListener("htmx:afterSwap", init);
})();
