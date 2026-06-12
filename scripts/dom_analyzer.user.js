// ==UserScript==
// @name         DOM分析器 - 通用页面结构分析
// @namespace    https://workbuddy.dev/dom-analyzer
// @version      1.0
// @description  分析任意页面的DOM结构：列表容器/条目/翻页/链接/内容区
// @author       WorkBuddy
// @match        *://*/*
// @grant        GM_download
// @run-at       document-end
// ==/UserScript==

(function () {
  'use strict';

  const PANEL_ID = '__dom_analyzer_panel__';

  // ============================================================
  // UI
  // ============================================================
  function injectStyles() {
    const css = `
#${PANEL_ID} {
  position: fixed; top: 80px; right: 16px; z-index: 99998;
  width: 380px; max-height: 80vh; background: #fff; border-radius: 10px;
  box-shadow: 0 4px 24px rgba(0,0,0,.15); font-size: 13px;
  font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  display: flex; flex-direction: column;
}
#${PANEL_ID} .hd {
  padding: 12px 14px; background: #1a1a2e; color: #fff;
  border-radius: 10px 10px 0 0; font-weight: 700;
  display: flex; justify-content: space-between; align-items: center;
  cursor: move; flex-shrink: 0;
}
#${PANEL_ID} .hd .min { cursor: pointer; font-size: 16px; line-height: 1; }
#${PANEL_ID} .bd {
  padding: 12px 14px; display: flex; flex-direction: column; gap: 8px;
  overflow-y: auto; flex: 1;
}
#${PANEL_ID} .bd.collapsed { display: none; }
#${PANEL_ID} button {
  width: 100%; padding: 8px 0; border: none; border-radius: 6px;
  font-size: 13px; cursor: pointer; font-weight: 600;
}
#${PANEL_ID} .btn-main { background: #1a73e8; color: #fff; }
#${PANEL_ID} .btn-main:hover { background: #1557b0; }
#${PANEL_ID} .btn-dl { background: #34a853; color: #fff; display: none; }
#${PANEL_ID} .btn-dl:hover { background: #2d8e47; }
#${PANEL_ID} .btn-close { background: #f0f0f0; color: #555; }
#${PANEL_ID} .btn-close:hover { background: #e0e0e0; }
#${PANEL_ID} .result {
  background: #f8f8f8; border: 1px solid #e0e0e0; border-radius: 6px;
  padding: 10px; font-size: 11px; max-height: 300px; overflow-y: auto;
  white-space: pre-wrap; font-family: Consolas,'Courier New',monospace;
  display: none; line-height: 1.5;
}
#${PANEL_ID} .status { font-size: 12px; color: #666; min-height: 18px; text-align: center; }
#${PANEL_ID} .tabs { display: flex; gap: 4px; }
#${PANEL_ID} .tabs button {
  flex: 1; padding: 5px 0; font-size: 11px; border-radius: 4px;
  background: #f0f0f0; color: #555; font-weight: 400;
}
#${PANEL_ID} .tabs button.active { background: #1a73e8; color: #fff; }
#${PANEL_ID} .hint { font-size: 11px; color: #999; text-align: center; }`;
    document.head.appendChild(Object.assign(document.createElement('style'), { textContent: css }));
  }

  function buildPanel() {
    const panel = document.createElement('div');
    panel.id = PANEL_ID;
    panel.innerHTML = `
<div class="hd"><span>DOM分析器</span><span class="min">−</span></div>
<div class="bd">
  <div class="tabs">
    <button class="active" data-tab="list">列表结构</button>
    <button data-tab="full">完整报告</button>
    <button data-tab="text">文本预览</button>
  </div>
  <button class="btn-main">🔍 分析当前页面</button>
  <div class="result"></div>
  <button class="btn-dl">📄 下载分析结果 (.txt)</button>
  <div class="status">点「分析」开始</div>
  <div class="hint">快捷键: Ctrl+Shift+D 打开面板</div>
  <button class="btn-close">关闭面板</button>
</div>`;
    document.body.appendChild(panel);

    // 折叠
    const min = panel.querySelector('.min'), bd = panel.querySelector('.bd');
    min.onclick = () => { const c = bd.classList.toggle('collapsed'); min.textContent = c ? '+' : '\u2212'; };

    // 拖拽
    let d = false, ox, oy;
    panel.querySelector('.hd').onmousedown = e => { if (e.target !== min) { d = true; ox = e.clientX - panel.offsetLeft; oy = e.clientY - panel.offsetTop; } };
    document.onmousemove = e => { if (d) { panel.style.left = (e.clientX - ox) + 'px'; panel.style.top = (e.clientY - oy) + 'px'; panel.style.right = 'auto'; } };
    document.onmouseup = () => { d = false; };

    return panel;
  }

  function S(panel, t) { panel.querySelector('.status').textContent = t; }

  // ============================================================
  // 核心分析
  // ============================================================
  let fullReport = '';
  let listSection = '';
  let textSection = '';
  let activeTab = 'list';

  function analyze(panel) {
    S(panel, '分析中...');
    const start = Date.now();

    // 等 SPA 稳定
    waitSPA().then(() => {
      const doc = { url: location.href, title: document.title, time: new Date().toLocaleString() };

      // 1. 列表结构
      const candidates = findLists();
      listSection = buildListReport(candidates);

      // 2. 翻页
      const pagers = findPagers();

      // 3. 主要内容区
      const mainZones = findMainZones();

      // 4. 链接
      const linkReport = buildLinkReport(candidates);

      // 5. 文本预览
      textSection = buildTextPreview(mainZones);

      // 完整报告
      fullReport = [
        `DOM结构分析报告`,
        `━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━`,
        `URL: ${doc.url}`,
        `标题: ${doc.title}`,
        `时间: ${doc.time}`,
        `耗时: ${Date.now() - start}ms`,
        ``,
        listSection,
        ``,
        `📄 【翻页/加载更多】`,
        pagers.length ? pagers.map(p => `  - "${p.text}" → ${p.selector}`).join('\n') : `  未找到`,
        ``,
        `📦 【主要内容区】(按文本量排序)`,
        mainZones.map((z, i) =>
          `  候选${i + 1}: ${z.selector}\n    文本量: ${z.textLen}字符 | 子元素: ${z.childCount}个\n    预览: ${z.preview}`
        ).join('\n'),
        ``,
        linkReport,
        ``,
        `💡 【建议】`,
        candidates.length > 0
          ? `  将 "条目选择器" 设为: ${candidates[0].itemSelector}`
          : `  未找到列表。尝试滚动页面后再分析，或用"文本预览"标签查看内容。`,
      ].join('\n');

      // 渲染当前 tab
      renderTab(panel);
      panel.querySelector('.btn-dl').style.display = 'block';
      S(panel, `完成！找到 ${candidates.length} 个列表结构`);
    }).catch(err => {
      S(panel, '分析出错: ' + err.message);
    });
  }

  // ============================================================
  // SPA 稳定等待
  // ============================================================
  function waitSPA() {
    return new Promise(resolve => {
      let last = document.body.innerHTML.length, stable = 0;
      const t = setInterval(() => {
        const cur = document.body.innerHTML.length;
        if (cur === last) stable++; else { stable = 0; last = cur; }
        if (stable >= 3) { clearInterval(t); resolve(); }
      }, 400);
      // 最多等 5 秒
      setTimeout(() => { clearInterval(t); resolve(); }, 5000);
    });
  }

  // ============================================================
  // 列表结构检测
  // ============================================================
  function findLists() {
    const results = [];
    const parentMap = new Map();

    document.querySelectorAll('*').forEach(el => {
      const p = el.parentElement;
      if (!p || p === document.body || p === document.documentElement) return;
      if (['script', 'style', 'meta', 'link', 'head', 'title', 'svg', 'path', 'g', 'defs'].includes(p.tagName.toLowerCase())) return;

      const pk = getKey(p);
      if (!parentMap.has(pk)) parentMap.set(pk, { parent: p, children: new Map() });
      parentMap.get(pk).children.set(getKey(el), (parentMap.get(pk).children.get(getKey(el)) || 0) + 1);
    });

    parentMap.forEach(entry => {
      entry.children.forEach((count, childKey) => {
        if (count < 3) return;
        const samples = Array.from(entry.parent.children).filter(c => getKey(c) === childKey);
        if (samples.length < 3) return;
        const sample = samples[0];
        const t = (sample.textContent || '').trim();
        if (t.length < 20) return;

        let score = 0;
        score += Math.min(count, 30);
        score += Math.min(t.length / 20, 15);
        score += sample.querySelector('a') ? 20 : 0;
        score += sample.querySelector('h1,h2,h3,h4,h5,h6') ? 15 : 0;
        score += sample.querySelector('img') ? 5 : 0;

        const internals = [];
        sample.querySelectorAll('h1,h2,h3,h4,h5,h6').forEach(h => {
          const ht = (h.textContent || '').trim();
          if (ht.length > 2) internals.push({ type: '标题', selector: buildSel(h), text: ht.substring(0, 60) });
        });
        sample.querySelectorAll('a').slice(0, 2).forEach(a => {
          const at = (a.textContent || '').trim();
          if (at.length > 3) internals.push({ type: '链接', selector: buildSel(a), text: at.substring(0, 60) });
        });
        sample.querySelectorAll('time,[class*="date"],[class*="time"]').forEach(d => {
          const dt = (d.textContent || '').trim();
          if (/\d{4}[-/]\d|今天|昨天/.test(dt)) internals.push({ type: '日期', selector: buildSel(d), text: dt });
        });

        results.push({
          containerSelector: buildSel(entry.parent),
          itemSelector: buildSel(sample),
          count, score,
          tag: sample.tagName.toLowerCase(),
          cls: safeClass(sample),
          sampleHTML: sample.outerHTML.substring(0, 400),
          internals,
        });
      });
    });

    // 去重排序
    const seen = new Set();
    return results
      .filter(r => { const k = r.containerSelector + '|' + r.itemSelector; if (seen.has(k)) return false; seen.add(k); return true; })
      .sort((a, b) => b.score - a.score)
      .slice(0, 10);
  }

  // ============================================================
  // 翻页检测
  // ============================================================
  function findPagers() {
    const keywords = ['下一页', '下一頁', '加载更多', '查看更多', '更多', 'next', 'Next', '>', '›'];
    const results = [];
    document.querySelectorAll('button,a,span,div,li').forEach(el => {
      const t = (el.textContent || '').trim();
      if (t.length > 0 && t.length < 20 && keywords.some(k => t.includes(k))) {
        results.push({ selector: buildSel(el), text: t });
      }
    });
    return results;
  }

  // ============================================================
  // 主要内容区
  // ============================================================
  function findMainZones() {
    const zones = [];
    const seen = new Set();
    document.querySelectorAll('div,section,article,main').forEach(el => {
      const t = (el.textContent || '').trim();
      if (t.length < 100 || t.length > 100000) return;
      // 跳过导航/侧边栏
      if (/(首页|导航|菜单|设置|登录|注册|退出|关于|帮助|联系)/.test(t.substring(0, 50)) && t.length < 200) return;
      const rect = el.getBoundingClientRect();
      if (rect.width < 200 || rect.height < 100) return;
      const sel = buildSel(el);
      if (seen.has(sel)) return; seen.add(sel);
      zones.push({
        selector: sel,
        textLen: t.length,
        childCount: el.children.length,
        preview: t.substring(0, 100).replace(/\s+/g, ' '),
      });
    });
    return zones.sort((a, b) => b.textLen - a.textLen).slice(0, 10);
  }

  // ============================================================
  // 链接分析
  // ============================================================
  function buildLinkReport(candidates) {
    const links = [];
    if (candidates.length > 0) {
      const sample = document.querySelector(candidates[0].itemSelector);
      if (sample) {
        sample.querySelectorAll('a').forEach(a => {
          const t = (a.textContent || '').trim();
          if (t.length > 3 && a.href && a.href !== location.href && !a.href.startsWith('javascript:')) {
            links.push({ text: t.substring(0, 60), href: a.href });
          }
        });
      }
    }
    if (!links.length) {
      // 全局搜
      document.querySelectorAll('a[href]').forEach(a => {
        const t = (a.textContent || '').trim();
        if (t.length > 10 && a.href !== location.href && !a.href.startsWith('javascript:') && links.length < 10) {
          links.push({ text: t.substring(0, 60), href: a.href });
        }
      });
    }
    return `🔗 【内容链接】\n` + (links.length
      ? links.map(l => `  "${l.text}"\n  → ${l.href}`).join('\n')
      : `  未找到明显内容链接`);
  }

  // ============================================================
  // 文本预览
  // ============================================================
  function buildTextPreview(zones) {
    if (!zones.length) return '（无主要内容区）';
    const top = zones[0];
    const el = document.querySelector(top.selector);
    const text = el ? (el.textContent || '').replace(/\s+/g, ' ').trim() : '';
    return text.length > 3000 ? text.substring(0, 3000) + '\n\n... (截断，完整文本量: ' + text.length + ' 字符)' : text;
  }

  // ============================================================
  // 报告生成
  // ============================================================
  function buildListReport(candidates) {
    if (!candidates.length) return `📋 【候选列表结构】\n  未找到。可能原因:\n  - 页面是 SPA，内容尚未渲染\n  - 条目间 DOM 结构差异太大\n  - 条目数量 < 3\n  建议: 滚动页面加载更多内容后重新分析，或查看"文本预览"标签。`;

    return `📋 【候选列表结构】(共 ${candidates.length} 个)\n` + candidates.map((c, i) =>
      `\n候选 ${i + 1}  ·  置信度: ${c.score}  ·  ${c.count} 条\n` +
      `  容器: ${c.containerSelector}\n` +
      `  条目: ${c.itemSelector}\n` +
      `  条目标签: <${c.tag}>  类名: ${c.cls || '(无)'}\n` +
      `  首条 HTML: ${c.sampleHTML.replace(/\n/g, '↵')}\n` +
      (c.internals.length ? `  内部元素:\n` + c.internals.map(internal =>
        `    - ${internal.type}: "${internal.text}"  →  ${internal.selector}`
      ).join('\n') : `  内部元素: (未检测到明确结构)`)
    ).join('\n');
  }

  // ============================================================
  // 渲染
  // ============================================================
  function renderTab(panel) {
    const result = panel.querySelector('.result');
    result.style.display = 'block';
    switch (activeTab) {
      case 'list': result.textContent = listSection; break;
      case 'full': result.textContent = fullReport; break;
      case 'text': result.textContent = textSection; break;
    }
  }

  // ============================================================
  // 下载
  // ============================================================
  function downloadReport(panel) {
    const blob = new Blob(['\uFEFF' + fullReport], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = `DOM分析_${document.title.replace(/[/:*?"<>|]/g,'_').substring(0,30)||'report'}.txt`;
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    URL.revokeObjectURL(url);
    S(panel, '已下载');
  }

  // ============================================================
  // 工具函数
  // ============================================================
  function safeClass(el) {
    if (!el) return '';
    const cn = el.className;
    if (typeof cn === 'string') return cn;
    if (cn && typeof cn.baseVal === 'string') return cn.baseVal;
    return '';
  }

  function getKey(el) {
    const tag = el.tagName.toLowerCase();
    const cls = safeClass(el);
    return tag + (cls ? '.' + cls.split(/\s+/).filter(Boolean).join('.') : '');
  }

  function buildSel(el) {
    if (el.id) return '#' + el.id;
    const tag = el.tagName.toLowerCase();
    const cls = safeClass(el);
    if (cls) return tag + '.' + cls.split(/\s+/).filter(Boolean).join('.');
    return tag;
  }

  // ============================================================
  // 初始化
  // ============================================================
  function init() {
    injectStyles();
    const panel = buildPanel();

    panel.querySelector('.btn-main').onclick = () => analyze(panel);
    panel.querySelector('.btn-dl').onclick = () => downloadReport(panel);
    panel.querySelector('.btn-close').onclick = () => {
      panel.style.display = 'none';
      // 快捷键可重新打开
    };

    // Tab 切换
    panel.querySelectorAll('.tabs button').forEach(btn => {
      btn.onclick = function () {
        panel.querySelectorAll('.tabs button').forEach(b => b.classList.remove('active'));
        this.classList.add('active');
        activeTab = this.dataset.tab;
        if (fullReport) renderTab(panel);
      };
    });

    // 快捷键 Ctrl+Shift+D
    document.addEventListener('keydown', e => {
      if (e.ctrlKey && e.shiftKey && e.key === 'D') {
        e.preventDefault();
        panel.style.display = panel.style.display === 'none' ? '' : 'none';
      }
    });

    console.log('[DOM分析器] 就绪。快捷键 Ctrl+Shift+D 开关面板。');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => setTimeout(init, 1500));
  } else {
    setTimeout(init, 1500);
  }
})();
