// ==UserScript==
// @name         InfoFlowHub 网页剪藏
// @namespace    https://tb14.tail7b1e6d.ts.net/
// @version      1.1
// @description  Alt+S（或油猴菜单）把当前页面收进 InfoFlowHub 稍后读（人工补录）；标签由每日 8:10 自动流程统一打
// @author       WorkBuddy
// @match        *://*/*
// @exclude      https://tb14.tail7b1e6d.ts.net/*
// @exclude      http://127.0.0.1:18421/*
// @exclude      http://localhost:18421/*
// @noframes
// @grant        GM_xmlhttpRequest
// @grant        GM_registerMenuCommand
// @run-at       document-end
// ==/UserScript==

(function () {
  'use strict';

  // 本机浏览器优先走回环：不受 Tailscale 设备改名影响（曾因设备名从
  // laptop-qrpmtrh0 改成 tb14 导致旧域名失效）；连不上再退回 tailnet 域名。
  const ENDPOINTS = [
    'http://127.0.0.1:18421/actions/laterhub/add-link',
    'https://tb14.tail7b1e6d.ts.net/actions/laterhub/add-link',
  ];

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

  function sendTo(index) {
    if (index >= ENDPOINTS.length) {
      toast('剪藏失败（连不上 InfoFlowHub，检查服务与 Tailscale）', false);
      return;
    }
    GM_xmlhttpRequest({
      method: 'POST',
      url: ENDPOINTS[index],
      headers: { 'Content-Type': 'application/json' },
      data: JSON.stringify({ url: location.href, title: document.title }),
      timeout: 10000,
      onload(res) {
        // 服务已应答就不再换端点：非 200 是真实业务错误，换域名也一样
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
        sendTo(index + 1);
      },
      ontimeout() {
        sendTo(index + 1);
      },
    });
  }

  GM_registerMenuCommand('收进稍后读 (Alt+S)', () => sendTo(0));
  // capture 阶段监听：SPA 站点常在捕获/冒泡阶段拦截快捷键，这里抢先处理
  document.addEventListener(
    'keydown',
    (event) => {
      if (event.altKey && !event.ctrlKey && !event.metaKey && event.key.toLowerCase() === 's') {
        event.preventDefault();
        sendTo(0);
      }
    },
    true
  );
})();
