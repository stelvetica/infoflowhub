// ==UserScript==
// @name         InfoFlowHub 抖音取链
// @namespace    infoflowhub.local
// @version      1.1.0
// @description  把当前抖音视频页的播放直链（videoDetail / aweme_detail 里的 play_addr）写进 DOM，供本地 InfoFlowHub 读取
// @match        https://www.douyin.com/*
// @run-at       document-start
// @grant        none
// ==/UserScript==
/*
 * 为什么需要它：bsk 的 evaluate 要先过「整页基线」校验，视频一播放就整页在变，动作被
 * 反复拒绝（实测长视频 8/8 被拒），所以取链只能退化成读网络缓冲（丢多 CDN、拿到的是分离流）。
 * 用户脚本跑在页面自己里，不受这道闸门约束：把直链写进 <meta id="ifh-douyin-media">，
 * 本地用 bsk get-html 读出来即可（get-html 同样不需要协作上下文）。
 *
 * 数据契约（本地解析方按此读取，改动需同步 connectors/douyin/video.py）：
 *   <meta id="ifh-douyin-media" content="<base64(UTF-8 JSON)>">
 *   JSON: {"current": <当前页视频条目|null>,
 *          "entries": [{"id": "<aweme_id>", "desc": "...", "playUrls": ["https://..."],
 *                       "subtitleUrls": ["https://..."],
 *                       "gears": [{"gear": "normal_1080_0", "bit": 1200, "url": "https://..."}],
 *                       "at": 1794283...}]}
 *   current 是 URL 里的视频，永不参与淘汰；entries 只留最近 5 条（多用于人工排查）。
 *   subtitleUrls 是作品自带字幕（作者上传或抖音自动生成）的下载地址，本地优先用它、
 *   有字幕就不再跑语音转写；空数组表示这条没有自带字幕。
 *
 * 安装（一次性）：Chrome 里装 Tampermonkey → 扩展详情页打开「允许用户脚本」→
 * 新建脚本，粘贴本文件全部内容并保存。装好后无需重启服务：取链时读到标记就用它，
 * 读不到自动退回 bsk 网络缓冲，不装也能正常跑（只是少了多 CDN 与合流直链）。
 */
(function () {
  'use strict';

  var MARKER_ID = 'ifh-douyin-media';
  var MAX_ENTRIES = 5;
  var entries = [];
  var currentEntry = null;  // 当前页 URL 指向的那条视频，单独留一个槽位

  function currentVideoId() {
    // 视频页形如 /video/<id>；分享弹层用 ?modal_id=<id>
    var match = String(location.pathname || '').match(/\/video\/(\d+)/) ||
      String(location.search || '').match(/[?&]modal_id=(\d+)/);
    return match ? match[1] : '';
  }

  function isMediaUrl(url) {
    return typeof url === 'string' && /^https:\/\/([a-z0-9.-]+\.)?(douyinvod\.com|douyin\.com)\//i.test(url);
  }

  function normalizeUrl(url) {
    // 老页面会给 http: 或协议相对地址，统一成 https
    if (typeof url !== 'string') return '';
    if (url.indexOf('//') === 0) return 'https:' + url;
    return url.replace(/^http:\/\//i, 'https://');
  }

  function isSubtitleUrl(url) {
    // 字幕在抖音各 CDN 上（douyinvod / ixigua / byteimg 等），本地解析方另有白名单，
    // 这里只挡掉非 https 与明显非法值。
    return typeof url === 'string' && /^https:\/\/[^\s"'<>]+$/i.test(url);
  }

  function subtitlesOf(video) {
    // subtitle_infos 是作品自带字幕轨道（作者上传或平台自动生成），有中文就优先用中文。
    var infos = video.subtitle_infos || video.subtitleInfos || [];
    if (!Array.isArray(infos)) return [];
    var zh = [];
    var other = [];
    for (var i = 0; i < infos.length; i++) {
      var s = infos[i];
      if (!s || typeof s !== 'object') continue;
      var url = normalizeUrl(s.Url || s.url || s.SubtitleUrl || '');
      if (!isSubtitleUrl(url)) continue;
      var lang = String(s.LanguageCodeName || s.language_code_name || s.Language || '').toLowerCase();
      (lang.indexOf('zh') === 0 || lang.indexOf('cmn') === 0 || lang.indexOf('chs') === 0 ? zh : other).push(url);
    }
    return zh.concat(other).slice(0, 3);
  }

  function fromAweme(aweme) {
    if (!aweme || typeof aweme !== 'object') return null;
    var video = aweme.video || aweme.videoInfo || {};
    var addr = video.play_addr || video.playAddr;
    var list = (addr && (addr.url_list || addr.urlList)) || [];
    list = list.map(normalizeUrl).filter(isMediaUrl);
    if (!list.length) return null;
    var gears = [];
    (video.bit_rate || video.bitRateList || []).forEach(function (rate) {
      var rAddr = rate.play_addr || rate.playAddr || {};
      var rList = ((rAddr.url_list || rAddr.urlList) || []).map(normalizeUrl).filter(isMediaUrl);
      if (rList.length) {
        gears.push({ gear: rate.gear_name || rate.gearName || '', bit: rate.bit_rate || rate.bitRate || 0, url: rList[0] });
      }
    });
    return {
      id: String(aweme.aweme_id || aweme.awemeId || ''),
      desc: String(aweme.desc || ''),
      playUrls: list.slice(0, 3),
      subtitleUrls: subtitlesOf(video),
      gears: gears.slice(0, 6),
      at: Date.now()
    };
  }

  function publish(entry) {
    if (!entry || !entry.id) return;
    // 相关推荐会源源不断刷进列表，把当前页视频挤出 5 条上限（实测 8 秒内就被顶掉），
    // 所以当前页视频单独存一个槽位，且不参与淘汰。
    if (entry.id === currentVideoId()) currentEntry = entry;
    entries = entries.filter(function (item) { return item.id !== entry.id; });
    entries.unshift(entry);
    entries = entries.slice(0, MAX_ENTRIES);

    var payload = null;
    try {
      payload = btoa(unescape(encodeURIComponent(JSON.stringify({ current: currentEntry, entries: entries }))));
    } catch (err) {
      return;
    }
    var root = document.head || document.documentElement;
    if (!root) return;
    var meta = document.getElementById(MARKER_ID);
    if (!meta) {
      meta = document.createElement('meta');
      meta.id = MARKER_ID;
      meta.setAttribute('name', MARKER_ID);
      root.appendChild(meta);
    }
    meta.setAttribute('content', payload);
  }

  function consumeAweme(aweme) {
    var entry = fromAweme(aweme);
    if (entry) publish(entry);
  }

  function consumeJson(data) {
    if (!data || typeof data !== 'object') return;
    if (data.aweme_detail) consumeAweme(data.aweme_detail);
    if (Array.isArray(data.aweme_list)) data.aweme_list.forEach(consumeAweme);
  }

  // ---- 通道 1：网络响应（videoDetail 的原始来源，字段最全） ----
  var nativeFetch = window.fetch;
  if (typeof nativeFetch === 'function') {
    window.fetch = function () {
      var args = arguments;
      return nativeFetch.apply(this, args).then(function (response) {
        try {
          var url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
          if (url.indexOf('/aweme/v1/web/') !== -1) {
            response.clone().json().then(consumeJson).catch(function () {});
          }
        } catch (err) { /* 观测失败不影响页面 */ }
        return response;
      });
    };
  }

  var nativeOpen = XMLHttpRequest.prototype.open;
  var nativeSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__ifhUrl = String(url || '');
    return nativeOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function () {
    var xhr = this;
    if (xhr.__ifhUrl && xhr.__ifhUrl.indexOf('/aweme/v1/web/') !== -1) {
      xhr.addEventListener('load', function () {
        try { consumeJson(JSON.parse(xhr.responseText)); } catch (err) { /* 非 JSON 忽略 */ }
      });
    }
    return nativeSend.apply(this, arguments);
  };

  // ---- 通道 2：React 树兜底（接口命中缓存、没有网络请求时） ----
  function scanReact() {
    var nodes = document.querySelectorAll('div, a, video');
    for (var i = 0; i < nodes.length && i < 600; i++) {
      var node = nodes[i];
      var key = Object.keys(node).filter(function (k) {
        return k.indexOf('__reactFiber$') === 0 || k.indexOf('__reactProps$') === 0;
      })[0];
      if (!key) continue;
      var fiber = node[key];
      for (var depth = 0; fiber && depth < 60; depth++, fiber = fiber.return) {
        var props = fiber.memoizedProps;
        if (!props || typeof props !== 'object') continue;
        var detail = props.videoDetail;
        if (detail && typeof detail === 'object') {
          var entry = fromAweme(detail);
          if (entry) return entry;
        }
      }
    }
    return null;
  }

  function tryReact() {
    try {
      var entry = scanReact();
      if (entry) publish(entry);
    } catch (err) { /* 页面结构变化不影响主流程 */ }
  }

  var scans = 0;
  var timer = setInterval(function () {
    tryReact();
    if (++scans >= 20) clearInterval(timer);  // 页面挂载后 30 秒仍没命中就放弃
  }, 1500);
  document.addEventListener('DOMContentLoaded', tryReact);
})();
