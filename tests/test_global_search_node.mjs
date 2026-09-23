// 全局搜索 handler 逻辑自检:无框架、最小 DOM stub,直接加载 app.js 触发 submit 回调。
// 运行: node tests/test_global_search_node.mjs
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, "..", "web", "static", "app.js"), "utf8");

let failed = 0;
function assert(cond, label) {
  if (cond) {
    console.log(`PASS ${label}`);
  } else {
    failed += 1;
    console.error(`FAIL ${label}`);
  }
}

const captured = { ajax: [], submit: null, cookies: [], requestSubmit: 0 };

function fakeInput(name, value = "") {
  return { type: "hidden", name, value };
}

function parseName(selector) {
  const match = /name="([^"]+)"/.exec(selector || "");
  return match ? match[1] : null;
}

function makeForm(fields) {
  const form = {
    dataset: {},
    _fields: fields,
    appendChild(el) {
      form._fields.push(el);
    },
    querySelector(selector) {
      const name = parseName(selector);
      return form._fields.find((el) => el.name === name) || null;
    },
    addEventListener() {},
  };
  return form;
}

const entriesForm = makeForm([fakeInput("entries_sort", "sort_time"), fakeInput("entries_dir", "desc")]);
const newsForm = makeForm([fakeInput("news_sort", "sort_time"), fakeInput("news_dir", "desc")]);
const laterhubQInputs = [fakeInput("laterhub_q", ""), fakeInput("laterhub_q", "")];
const laterhubStack = { requestSubmit() { captured.requestSubmit += 1; } };
const metaFragment = { dataset: { page: "3", unreadOnly: "1", readKeys: "k1" } };
const globalSearchInput = { value: " AI " };
const globalSearchForm = {
  dataset: {},
  addEventListener(evt, fn) {
    if (evt === "submit") captured.submit = fn;
  },
};

const laterhubPanel = {
  querySelectorAll(selector) {
    return selector === 'input[name="laterhub_q"]' ? laterhubQInputs : [];
  },
  querySelector(selector) {
    return selector === ".laterhub-form-stack" ? laterhubStack : null;
  },
};

globalThis.window = globalThis;
globalThis.localStorage = { getItem: () => null, setItem() {} };
globalThis.htmx = {
  ajax(method, url) {
    captured.ajax.push(url);
  },
};
globalThis.FormData = class {
  constructor(form) {
    this._fields = form._fields;
  }
  forEach(cb) {
    this._fields.forEach((el) => cb(el.value, el.name));
  }
};
globalThis.document = {
  getElementById(id) {
    if (id === "global-search-form") return globalSearchForm;
    if (id === "global-search") return globalSearchInput;
    if (id === "laterhub-panel") return laterhubPanel;
    return null;
  },
  querySelector(selector) {
    if (selector === ".entries-header-form") return entriesForm;
    if (selector === ".news-header-form") return newsForm;
    if (selector === "[data-entries-fragment]" || selector === "[data-news-fragment]") return metaFragment;
    return null;
  },
  querySelectorAll() {
    return [];
  },
  createElement() {
    return fakeInput("");
  },
  addEventListener(evt, fn) {
    if (evt === "DOMContentLoaded") captured.domReady = fn;
  },
  body: { addEventListener() {} },
  set cookie(value) {
    captured.cookies.push(value);
  },
  get cookie() {
    return "";
  },
};

new Function(source)();
captured.domReady();

// 用例1: 输入 " AI " 回车 → 三面板各自过滤,laterhub 表单联动
globalSearchInput.value = " AI ";
let preventDefaultCalled = false;
captured.submit({ preventDefault() { preventDefaultCalled = true; } });

const panelParams = {};
captured.ajax.forEach((url) => {
  const [path, qs] = url.split("?");
  panelParams[path] = new URLSearchParams(qs);
});

assert(preventDefaultCalled, "回车提交被拦截,不整页刷新");
assert(captured.ajax.length === 2, "新闻与订阅两个面板发起刷新");
assert(panelParams["/fragments/entries"]?.get("entries_q") === "AI", "订阅面板按关键词过滤");
assert(panelParams["/fragments/entries"]?.get("entries_after") === null, "订阅面板从首批重新取(不带续载游标)");
assert(panelParams["/fragments/entries"]?.get("entries_sort") === "sort_time", "订阅面板保留排序状态");
assert(panelParams["/fragments/news"]?.get("news_q") === "AI", "新闻面板按关键词过滤");
assert(laterhubQInputs.every((el) => el.value === "AI"), "关键词同步到稍后处理表单");
assert(captured.requestSubmit === 1, "稍后处理表单被提交刷新");
assert(globalSearchInput.value === "AI", "输入框规范化为首尾去空格的关键词");
assert(
  captured.cookies.some((c) => c.includes("entries_q=AI")),
  "关键词写入订阅状态 cookie",
);
assert(
  captured.cookies.some((c) => c.includes("news_q=AI")),
  "关键词写入新闻状态 cookie",
);

// 用例2: 清空再回车 → 不携带关键词,cookie 同步为空
captured.ajax.length = 0;
captured.cookies.length = 0;
globalSearchInput.value = "   ";
captured.submit({ preventDefault() {} });

const clearedParams = captured.ajax.map((url) => new URLSearchParams(url.split("?")[1]));
assert(captured.ajax.length === 2, "清空回车仍刷新两个面板");
assert(
  clearedParams.every((p) => !p.has("entries_q") && !p.has("news_q")),
  "清空回车后请求不携带关键词参数",
);
assert(laterhubQInputs.every((el) => el.value === ""), "清空同步到稍后处理表单");
assert(captured.requestSubmit === 2, "清空回车后稍后处理表单再次提交");
assert(
  captured.cookies.some((c) => c.includes("entries_q=&") || c.endsWith("entries_q=")),
  "空关键词写入订阅 cookie,服务端据此取消过滤",
);
assert(
  captured.cookies.some((c) => c.includes("news_q=&") || c.endsWith("news_q=")),
  "空关键词写入新闻 cookie,服务端据此取消过滤",
);

if (failed > 0) {
  console.error(`\n${failed} 项断言失败`);
  process.exit(1);
}
console.log("\n全局搜索逻辑自检全部通过");
