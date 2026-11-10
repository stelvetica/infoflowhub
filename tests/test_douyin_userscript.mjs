// 抖音取链用户脚本自检：最小 DOM/XHR stub 加载 scripts/douyin_media_export.user.js，
// 验证「读 aweme_detail → 过滤非法直链 → 写入 base64 标记」这条契约。
// 运行: node tests/test_douyin_userscript.mjs
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, "..", "scripts", "douyin_media_export.user.js"), "utf8");

let failed = 0;
function assert(cond, label) {
  if (cond) {
    console.log(`PASS ${label}`);
  } else {
    failed += 1;
    console.error(`FAIL ${label}`);
  }
}

const byId = new Map();
const head = {
  appendChild(el) {
    if (el.attrs && el.attrs.id) byId.set(el.attrs.id, el);
  },
};
const documentStub = {
  head,
  documentElement: head,
  addEventListener() {},
  querySelectorAll() {
    return [];
  },
  getElementById(id) {
    return byId.get(id) || null;
  },
  createElement(tag) {
    const el = {
      tagName: String(tag).toUpperCase(),
      attrs: {},
      setAttribute(key, value) {
        this.attrs[key] = value;
        if (key === "id") byId.set(value, this);
      },
      getAttribute(key) {
        return this.attrs[key] ?? null;
      },
    };
    // 真 DOM 里 el.id = x 会反射到 id 属性，stub 必须照做，否则测不到标记注册
    Object.defineProperty(el, "id", {
      get() {
        return this.attrs.id;
      },
      set(value) {
        this.attrs.id = value;
        byId.set(value, this);
      },
    });
    return el;
  },
};

class FakeXHR {
  constructor() {
    this.listeners = Object.create(null);
    this.responseText = "";
  }

  open(method, url) {
    this.__url = url;
  }

  send() {}

  addEventListener(type, handler) {
    (this.listeners[type] ||= []).push(handler);
  }

  emit(type) {
    (this.listeners[type] || []).forEach((handler) => handler.call(this));
  }
}

const context = {
  window: {},
  document: documentStub,
  XMLHttpRequest: FakeXHR,
  location: { pathname: "/video/7664439615676275451", search: "" },
  setInterval: () => 1,
  clearInterval: () => {},
  btoa: (text) => Buffer.from(text, "binary").toString("base64"),
  unescape,
  console,
};
vm.createContext(context);
vm.runInContext(source, context);

const fixture = {
  aweme_id: "7664439615676275451",
  desc: "测试文案",
  video: {
    play_addr: {
      url_list: [
        "http://v26-web.douyinvod.com/a/video/tos/cn/x/?a=1&b=2",
        "https://malicious.example/v.mp4",
      ],
    },
    bit_rate: [
      {
        gear_name: "normal_1080_0",
        bit_rate: 1141,
        play_addr: { url_list: ["https://v26-web.douyinvod.com/hi/video/tos/cn/y/?a=1"] },
      },
    ],
  },
};

const xhr = new FakeXHR();
xhr.open("GET", "https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=7664439615676275451");
xhr.send();
xhr.responseText = JSON.stringify({ aweme_detail: fixture });
xhr.emit("load");

const marker = byId.get("ifh-douyin-media");
assert(!!marker, "写入了 ifh-douyin-media 标记");
const packed = marker && marker.getAttribute("content");
assert(typeof packed === "string" && packed.length > 0, "标记内容是 base64 字符串");

let payload = null;
try {
  payload = JSON.parse(Buffer.from(packed, "base64").toString("utf8"));
} catch (err) {
  payload = null;
}
assert(!!payload && Array.isArray(payload.entries), "base64 能解出 entries 数组");

const entry = payload && payload.entries[0];
assert(!!entry && entry.id === "7664439615676275451", "条目带正确的 aweme id");
assert(Array.isArray(entry.playUrls) && entry.playUrls.length === 1, "非法域名被过滤，只留 1 条直链");
assert(entry.playUrls[0].startsWith("https://v26-web.douyinvod.com/"), "http 直链被升级为 https");
assert(entry.playUrls[0].includes("a=1&b=2"), "查询串保留完整签名");
assert(Array.isArray(entry.gears) && entry.gears[0].bit === 1141, "bit_rate 档位一并导出");
assert(typeof entry.at === "number" && entry.at > 0, "条目带时间戳");
assert(!!payload.current && payload.current.id === "7664439615676275451", "当前页视频写入 current 槽位");

// 相关推荐刷屏（>5 条）后，当前页视频仍在 current 里
function publishAweme(awemeId) {
  const noise = new FakeXHR();
  noise.open("GET", `https://www.douyin.com/aweme/v1/web/aweme/post/?id=${awemeId}`);
  noise.send();
  noise.responseText = JSON.stringify({
    aweme_detail: {
      aweme_id: awemeId,
      desc: `推荐 ${awemeId}`,
      video: { play_addr: { url_list: [`https://v26-web.douyinvod.com/${awemeId}/video/tos/cn/z/`] } },
    },
  });
  noise.emit("load");
}
for (let i = 0; i < 8; i += 1) publishAweme(`70000000000000000${i}`);

const after = JSON.parse(Buffer.from(byId.get("ifh-douyin-media").getAttribute("content"), "base64").toString("utf8"));
assert(after.entries.length === 5, "entries 只保留最近 5 条");
assert(after.entries[0].id !== "7664439615676275451", "目标视频已被推荐挤出 entries（复现原问题）");
assert(!!after.current && after.current.id === "7664439615676275451", "current 槽位不受淘汰影响");
assert(after.current.playUrls[0].startsWith("https://v26-web.douyinvod.com/"), "current 直链完整保留");

// 没有可用直链时不得写入标记
byId.clear();
const broken = new FakeXHR();
broken.open("GET", "https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=2");
broken.send();
broken.responseText = JSON.stringify({ aweme_detail: { aweme_id: "2", video: { play_addr: { url_list: [] } } } });
broken.emit("load");
assert(!byId.get("ifh-douyin-media"), "无直链时不写标记");

if (failed) {
  console.error(`${failed} check(s) failed`);
  process.exit(1);
}
console.log("all checks passed");
