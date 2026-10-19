// ==UserScript==
// @name         InfoFlowHub 网页剪藏
// @namespace    https://laptop-qrpmtrh0.tail7b1e6d.ts.net/
// @version      1.0
// @description  Alt+S（或油猴菜单）把当前页面收进 InfoFlowHub 稍后读（人工补录）；标签由每日 8:20 自动流程统一打
// @author       WorkBuddy
// @match        *://*/*
// @exclude      https://laptop-qrpmtrh0.tail7b1e6d.ts.net/*
// @exclude      http://127.0.0.1:18421/*
// @exclude      http://localhost:18421/*
// @noframes
// @grant        GM_xmlhttpRequest
// @grant        GM_registerMenuCommand
// @run-at       document-end
// ==/UserScript==

(function () {
  'use strict';

  // tailnet 内任何机器（含本机）都可达；Tailscale serve 持久代理到 127.0.0.1:18421
  const ENDPOINT = 'https://laptop-qrpmtrh0.tail7b1e6d.ts.net/actions/laterhub/add-link';

  function toast(message, ok) {
    const el = document.createElement('div');
    el.textContent = message;
    el.style.cssText =
      'position:fixed;top:20px;right:20px;z-index:2147483647;padding:10px 16px;' +
      'border-radius:8px;font-size:13px;font-family:system-ui,sans-serif;color:#fff;' +
      `background:${ok ? '#16a34a' : '#dc2626'};box-shadow:0 4px 16px rgba(0,0,0,.25);`;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 2500);
  }

  function send() {
    GM_xmlhttpRequest({
      method: 'POST',
      url: ENDPOINT,
      headers: { 'Content-Type': 'application/json' },
      data: JSON.stringify({ url: location.href, title: document.title }),
      timeout: 10000,
      onload(res) {
        if (res.status !== 200) {
          toast(`剪藏失败（HTTP ${res.status}）`, false);
          return;
        }
        try {
          const body = JSON.parse(res.responseText);
          toast(body.duplicated ? '已在稍后读中（重复收录）' : '已收进稍后读', true);
        } catch (err) {
          toast('剪藏失败（响应异常）', false);
        }
      },
      onerror() {
        toast('剪藏失败（连不上 InfoFlowHub，检查 Tailscale）', false);
      },
      ontimeout() {
        toast('剪藏失败（连接超时）', false);
      },
    });
  }

  GM_registerMenuCommand('收进稍后读 (Alt+S)', send);
  // capture 阶段监听：SPA 站点常在捕获/冒泡阶段拦截快捷键，这里抢先处理
  document.addEventListener(
    'keydown',
    (event) => {
      if (event.altKey && !event.ctrlKey && !event.metaKey && event.key.toLowerCase() === 's') {
        event.preventDefault();
        send();
      }
    },
    true
  );
})();
