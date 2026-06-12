// ==UserScript==
// @name         Alpha派 - 蓝宝书批量下载
// @namespace    https://alphapai-web.rabyte.cn/
// @version      2.3
// @description  免责声明锚点检测 + HTML→Markdown
// @author       WorkBuddy
// @match        https://alphapai-web.rabyte.cn/reading/home/market-report/detail
// @grant        GM_download
// @run-at       document-end
// ==/UserScript==

(function () {
  'use strict';

  const PANEL_ID = '__alpha_dl_panel__';
  let allEntries = [];
  let collectedResults = [];
  let isRunning = false;

  function injectStyles() {
    const css = `
#${PANEL_ID} {
  position: fixed; top: 80px; right: 16px; z-index: 99999;
  width: 360px; background: #fff; border-radius: 10px;
  box-shadow: 0 4px 24px rgba(0,0,0,.15); font-size: 13px;
  font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
}
#${PANEL_ID} .hd {
  padding: 12px 14px; background: #1a1a2e; color: #fff;
  border-radius: 10px 10px 0 0; font-weight: 700;
  display: flex; justify-content: space-between; align-items: center;
  cursor: move;
}
#${PANEL_ID} .hd .min { cursor: pointer; font-size: 16px; line-height: 1; }
#${PANEL_ID} .bd { padding: 12px 14px; display: flex; flex-direction: column; gap: 8px; max-height: 70vh; overflow-y: auto; }
#${PANEL_ID} .bd.collapsed { display: none; }
#${PANEL_ID} button {
  width: 100%; padding: 9px 0; border: none; border-radius: 6px;
  font-size: 13px; cursor: pointer; font-weight: 600;
}
#${PANEL_ID} .btn-scan { background: #e8f0fe; color: #1a73e8; }
#${PANEL_ID} .btn-scan:hover { background: #d2e3fc; }
#${PANEL_ID} .btn-deep { background: #ea4335; color: #fff; }
#${PANEL_ID} .btn-deep:hover { background: #d33426; }
#${PANEL_ID} .btn-deep:disabled { background: #ccc; cursor: not-allowed; }
#${PANEL_ID} .btn-stop { background: #f0ad4e; color: #fff; display: none; }
#${PANEL_ID} .btn-stop:hover { background: #ec971f; }
#${PANEL_ID} .btn-dl { background: #34a853; color: #fff; }
#${PANEL_ID} .btn-dl:hover { background: #2d8e47; }
#${PANEL_ID} .btn-dl:disabled { background: #ccc; cursor: not-allowed; }
#${PANEL_ID} .preview {
  background: #f8f8f8; border: 1px solid #e0e0e0; border-radius: 6px;
  padding: 8px; font-size: 11px; max-height: 180px; overflow-y: auto;
  white-space: pre-wrap; font-family: monospace; display: none;
}
#${PANEL_ID} .status { font-size: 12px; color: #333; min-height: 18px; }
#${PANEL_ID} .progress { height: 4px; background: #e0e0e0; border-radius: 2px; overflow: hidden; }
#${PANEL_ID} .progress-bar { height: 100%; width: 0; background: #1a73e8; transition: width .3s; }
#${PANEL_ID} .count { font-size: 12px; color: #1a73e8; font-weight: 600; text-align: center; display: none; }
#${PANEL_ID} .select-row { display: flex; gap: 4px; align-items: center; font-size: 12px; }
#${PANEL_ID} .select-row input { width: 60px; padding: 4px; border: 1px solid #ddd; border-radius: 4px; text-align: center; }
#${PANEL_ID} .select-row span { color: #666; }`;
    document.head.appendChild(Object.assign(document.createElement('style'), { textContent: css }));
  }

  function buildPanel() {
    const panel = document.createElement('div');
    panel.id = PANEL_ID;
    panel.innerHTML = `
<div class="hd"><span>蓝宝书 v2.3</span><span class="min">−</span></div>
<div class="bd">
  <button class="btn-scan">🔍 步骤1: 扫描报告列表</button>
  <div class="count"></div>
  <div class="select-row">
    <span>范围:</span><input id="rfrom" value="1"><span>-</span><input id="rto" value="3">
  </div>
  <button class="btn-deep" disabled>⏳ 步骤2: 逐条提取正文</button>
  <button class="btn-stop">⏹ 停止</button>
  <div class="progress"><div class="progress-bar"></div></div>
  <div class="preview"></div>
  <button class="btn-dl" disabled>📄 步骤3: 下载(.md格式)</button>
  <div class="status">点步骤1开始</div>
</div>`;
    document.body.appendChild(panel);
    const min = panel.querySelector('.min'), bd = panel.querySelector('.bd');
    min.onclick = () => { const c = bd.classList.toggle('collapsed'); min.textContent = c ? '+' : '\u2212'; };
    let d = false, ox, oy;
    panel.querySelector('.hd').onmousedown = e => { if (e.target !== min) { d = true; ox = e.clientX - panel.offsetLeft; oy = e.clientY - panel.offsetTop; } };
    document.onmousemove = e => { if (d) { panel.style.left = (e.clientX - ox) + 'px'; panel.style.top = (e.clientY - oy) + 'px'; panel.style.right = 'auto'; } };
    document.onmouseup = () => { d = false; };
    return panel;
  }

  function S(panel, t) { panel.querySelector('.status').textContent = t; }
  function P(panel, p) { panel.querySelector('.progress-bar').style.width = Math.min(100, p) + '%'; }

  // ============================================================
  // 步骤1: 扫描
  // ============================================================
  function scan(panel) {
    S(panel, '扫描中...');
    allEntries = []; collectedResults = [];
    const body = document.querySelector('div.app-layout-body') || document.body;
    const t = (body.textContent || '').replace(/\s+/g, ' ').trim();
    const re = /(全球|国内)(\d+)月(\d+)日((?:全球版|晨会版|晚间版|午间版))\|\s*([\s\S]+?)(今天|昨天|\d{1,2}:\d{1,2}|\d{1,2}-\d{1,2}\s\d{1,2}:\d{1,2})/g;
    let m;
    while ((m = re.exec(t))) allEntries.push({ region: m[1], month: m[2], day: m[3], edition: m[4], summary: m[5].trim(), time: m[6], date: m[2] + '月' + m[3] + '日' });
    if (!allEntries.length) { S(panel, '未扫到。先滚动加载更多。'); return; }
    const pv = panel.querySelector('.preview'); pv.style.display = 'block';
    pv.textContent = allEntries.map((e, i) => `[${i + 1}] ${e.region} ${e.date} ${e.edition}\n     ${e.summary.substring(0, 80)}`).join('\n\n');
    panel.querySelector('.count').style.display = 'block';
    panel.querySelector('.count').textContent = `共 ${allEntries.length} 条`;
    panel.querySelector('#rto').value = allEntries.length;
    panel.querySelector('.btn-deep').disabled = false;
    S(panel, `扫到 ${allEntries.length} 条。范围可调，然后步骤2`);
  }

  // ============================================================
  // 步骤2: 深度提取
  // ============================================================
  async function deep(panel) {
    if (isRunning || !allEntries.length) return;
    const f = +panel.querySelector('#rfrom').value || 1;
    const t = +panel.querySelector('#rto').value || allEntries.length;
    const range = allEntries.slice(f - 1, Math.min(t, allEntries.length));
    if (!range.length) { S(panel, '范围无效'); return; }

    isRunning = true; collectedResults = [];
    panel.querySelector('.btn-deep').disabled = true;
    panel.querySelector('.btn-stop').style.display = 'block';
    panel.querySelector('.btn-dl').disabled = true;

    for (let i = 0; i < range.length; i++) {
      if (!isRunning) break;
      const e = range[i];
      const idx = f + i;
      S(panel, `${i + 1}/${range.length}: ${e.region} ${e.date} ${e.edition}`);
      P(panel, Math.round(i / range.length * 100));

      try {
        const detail = await extractOne(e);
        collectedResults.push({ ...e, fullContent: detail || '⚠ 未提取到正文（点击或定位失败）' });
        console.log(`[${idx}] ✓ ${e.region} ${e.edition} → ${(detail || '').length} 字符`);
      } catch (err) {
        collectedResults.push({ ...e, fullContent: '⚠ 异常: ' + err.message });
        console.error(`[${idx}] ✗ ${e.region} ${e.edition}`, err);
      }

      const pv = panel.querySelector('.preview');
      pv.style.display = 'block';
      pv.textContent = collectedResults.map((r, j) =>
        `[${j + 1}] ${r.region} ${r.date} ${r.edition}: ${((r.fullContent || '').length > 30 ? '✓ ' + String(r.fullContent).length + '字' : '✗ 失败')}`
      ).join('\n');
    }

    isRunning = false; P(panel, 100);
    panel.querySelector('.btn-deep').disabled = false;
    panel.querySelector('.btn-stop').style.display = 'none';
    panel.querySelector('.btn-dl').disabled = !collectedResults.length;
    S(panel, `完成！成功 ${collectedResults.filter(r => r.fullContent && r.fullContent.length > 30).length}/${collectedResults.length}`);
  }

  async function extractOne(entry) {
    const body = document.querySelector('div.app-layout-body');
    if (!body) return null;

    // 1. 找可点击元素
    const clickable = findClickable(entry);
    if (!clickable) { console.warn('未找到可点击元素:', entry.summary.substring(0, 30)); return null; }

    const before = body.innerHTML;
    clickable.click();
    console.log('点击:', entry.summary.substring(0, 50));

    // 2. 等待 DOM 稳定（用 innerHTML 长度对比，v2.0 验证有效）
    const detailHTML = await waitStable(body, before);
    if (!detailHTML) { console.warn('详情未加载'); return null; }

    // 3. 精确定位详情容器
    const container = findDetailContainer(body);
    const sourceHTML = container ? container.innerHTML : detailHTML;

    // 4. HTML → 格式化文本
    const formatted = htmlToMarkdown(sourceHTML);

    // 5. 返回列表
    await goBack(body);

    return formatted;
  }

  function findClickable(entry) {
    const body = document.querySelector('div.app-layout-body') || document.body;
    const kw = entry.summary.substring(0, 15).replace(/[|.*+?^${}()[\]\\]/g, '\\$&');
    try {
      const r = document.evaluate(`.//*[contains(text(),'${kw}')]`, body, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
      let el = r.singleNodeValue;
      if (!el) return null;
      // 向上找列表条目容器
      while (el && el !== body) {
        const p = el.parentElement;
        if (!p || p === body) break;
        // 检查父元素是否包含多个同类子元素 = 是列表容器
        const cls = typeof el.className === 'string' ? el.className : '';
        const sel = el.tagName.toLowerCase() + (cls ? '.' + cls.trim().split(/\s+/).join('.') : '');
        try { if (p.querySelectorAll(':scope > ' + sel).length > 1) { el = p; continue; } } catch (_) {}
        break;
      }
      return el;
    } catch (_) { return null; }
  }

  function waitStable(body, beforeHTML) {
    return new Promise(resolve => {
      const start = Date.now();
      const timeout = 15000;
      const disclaimerKeys = ['免责', '免责申明', '申明', '不构成任何投资建议', '投资建议'];
      let last = beforeHTML, stable = 0;
      const t = setInterval(() => {
        const cur = body.innerHTML;
        const curText = body.textContent || '';
        const hasDisclaimer = disclaimerKeys.some(k => curText.includes(k));

        if (cur !== beforeHTML && cur === last) { stable++; }
        else if (cur !== last) { stable = 0; last = cur; }

        // 必须同时满足：HTML已变化 + 连续稳定 + 看到免责声明标记
        const done = stable >= 3 && hasDisclaimer;
        const timedOut = Date.now() - start > timeout;

        if (done || timedOut) {
          clearInterval(t);
          if (done) console.log('✓ 加载完成（已检测到免责声明）');
          else if (timedOut && !hasDisclaimer) console.warn('⚠ 超时，未检测到免责声明');
          resolve(done ? cur : null);
        }
      }, 300);
    });
  }

  function findDetailContainer(body) {
    const markers = ['分享播放时长', '播放时长', '聚合并生成', '市场热点', '机会前瞻'];
    const all = Array.from(body.querySelectorAll('*'));
    let best = null, bestScore = 0;
    for (const el of all) {
      const txt = (el.textContent || '').trim();
      const len = txt.length;
      if (len < 500 || len > 60000) continue;
      const score = markers.filter(m => txt.includes(m)).length;
      if (score > bestScore && !txt.startsWith('全部国内全球')) {
        bestScore = score; best = el;
      }
    }
    return best;
  }

  async function goBack(body) {
    const sels = ['[class*="back"]', '[class*="close"]', '.el-icon-close', '.el-drawer__close-btn', '.el-icon-arrow-left'];
    for (const s of sels) {
      try {
        const b = document.querySelector(s);
        if (b && b.offsetParent) { b.click(); console.log('返回:', s); await sleep(2000); return; }
      } catch (_) {}
    }
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', keyCode: 27, bubbles: true }));
    await sleep(2000);
  }

  // ============================================================
  // HTML → Markdown
  // ============================================================
  function htmlToMarkdown(html) {
    const div = document.createElement('div');
    div.innerHTML = html;
    div.querySelectorAll('script,style,svg,img,video,audio,iframe').forEach(e => e.remove());
    const lines = [];
    walk(div, lines);
    return lines.join('\n').replace(/\n{4,}/g, '\n\n\n').replace(/^[ \t]+/gm, '').trim();
  }

  function walk(node, lines) {
    if (!node) return;
    if (node.nodeType === 3) {
      const t = node.textContent.replace(/\s+/g, ' ').trim();
      if (t) {
        const last = lines[lines.length - 1] || '';
        if (last && !/[\n。：:）\)\-]$/.test(last) && !t.startsWith('**')) {
          lines[lines.length - 1] = last + t;
        } else { lines.push(t); }
      }
      return;
    }
    if (node.nodeType !== 1) return;
    const tag = node.tagName.toLowerCase();
    const txt = (node.textContent || '').trim();
    if (!txt) return;

    // 跳过列表导航
    if (tag === 'div' && /^全部[国内全球]+$/.test(txt.substring(0, 30))) return;

    // 块级
    if (['h1', 'h2'].includes(tag)) { lines.push('\n## ' + txt + '\n'); return; }
    if (['h3', 'h4'].includes(tag)) { lines.push('\n### ' + txt + '\n'); return; }
    if (['h5', 'h6'].includes(tag)) { lines.push('\n#### ' + txt + '\n'); return; }
    if (tag === 'p') { const t = inline(node); if (t) lines.push('\n' + t); return; }
    if (tag === 'li') { const t = inline(node); if (t) lines.push('- ' + t); return; }
    if (tag === 'br') { lines.push(''); return; }
    if (tag === 'hr') { lines.push('\n---\n'); return; }

    // 内联格式
    if (['strong', 'b'].includes(tag)) { lastAppend(lines, '**' + txt + '**'); return; }
    if (['em', 'i'].includes(tag)) { lastAppend(lines, '*' + txt + '*'); return; }

    // 容器
    const blocks = ['p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li', 'section', 'article', 'blockquote'];
    const hasBlock = Array.from(node.children).some(c => blocks.includes(c.tagName.toLowerCase()));
    if (hasBlock) { Array.from(node.childNodes).forEach(c => walk(c, lines)); }
    else if (txt.length > 8) { lines.push(txt); }
  }

  function inline(node) {
    const parts = [];
    (function w(n) {
      if (n.nodeType === 3) { const t = n.textContent.replace(/\s+/g, ' '); if (t.trim()) parts.push(t); return; }
      if (n.nodeType !== 1) return;
      const tg = n.tagName.toLowerCase();
      if (['strong', 'b'].includes(tg)) parts.push('**' + (n.textContent || '').replace(/\s+/g, ' ').trim() + '**');
      else if (['em', 'i'].includes(tg)) parts.push('*' + (n.textContent || '').replace(/\s+/g, ' ').trim() + '*');
      else if (tg === 'br') parts.push('\n');
      else Array.from(n.childNodes).forEach(w);
    })(node);
    return parts.join('').trim();
  }

  function lastAppend(lines, s) {
    if (!lines.length) lines.push('');
    lines[lines.length - 1] += s;
  }

  // ============================================================
  // 停止
  // ============================================================
  function stop(panel) {
    isRunning = false;
    S(panel, `已停止。收集 ${collectedResults.length} 条`);
    panel.querySelector('.btn-deep').disabled = false;
    panel.querySelector('.btn-stop').style.display = 'none';
    panel.querySelector('.btn-dl').disabled = !collectedResults.length;
  }

  // ============================================================
  // 步骤3: 下载
  // ============================================================
  function download(panel) {
    const data = collectedResults.length ? collectedResults : allEntries;
    if (!data.length) { S(panel, '无内容'); return; }

    const text = data.map((e, i) => {
      const body = e.fullContent || e.summary || '';
      return '═'.repeat(60) + '\n## ' + e.region + ' ' + e.date + ' ' + e.edition +
        '\n**时间:** ' + e.time + '\n' + '═'.repeat(60) + '\n\n' + body + '\n\n';
    }).join('');

    const first = (data[0].date || '').replace(/月/g, '-').replace(/日/g, '');
    const last = (data[data.length - 1].date || '').replace(/月/g, '-').replace(/日/g, '');
    const tag = collectedResults.length ? '完整版' : '摘要版';
    const blob = new Blob(['\uFEFF' + text], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = url; a.download = `Alpha派_蓝宝书_${tag}_${first}_${last}.md`;
    document.body.appendChild(a); a.click(); document.body.removeChild(a); URL.revokeObjectURL(url);
    S(panel, `下载完成: ${data.length} 条`);
  }

  function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

  // ============================================================
  // 初始化
  // ============================================================
  function init() {
    injectStyles();
    const panel = buildPanel();
    panel.querySelector('.btn-scan').onclick = () => scan(panel);
    panel.querySelector('.btn-deep').onclick = () => deep(panel);
    panel.querySelector('.btn-stop').onclick = () => stop(panel);
    panel.querySelector('.btn-dl').onclick = () => download(panel);
    console.log('[蓝宝书 v2.3] 就绪');
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => setTimeout(init, 2500));
  else setTimeout(init, 2500);
})();
