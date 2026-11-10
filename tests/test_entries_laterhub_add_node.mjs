// 订阅条目「加入稍后读」按钮自检：无框架、最小 DOM stub，直接加载 app.js 驱动点击。
// 核心回归：点一次只发一个 JSON 收链请求，按钮就地反馈，右栏按当前筛选刷新。
// 运行: node tests/test_entries_laterhub_add_node.mjs
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

const captured = { domReady: null, afterSwap: [], fetches: [], ajax: [], timers: [] };

function makeClassList() {
  const set = new Set();
  return {
    add: (c) => set.add(c),
    remove: (c) => set.delete(c),
    contains: (c) => set.has(c),
    toggle: (c, on) => (on === undefined ? (set.has(c) ? set.delete(c) : set.add(c)) : on ? set.add(c) : set.delete(c)),
  };
}

function makeAddButton(url = "/markdown/alphapai/a.md", title = "全球蓝宝书") {
  const listeners = [];
  const button = {
    dataset: { addUrl: url, addTitle: title },
    textContent: "加入",
    disabled: false,
    classList: makeClassList(),
    addEventListener(evt, fn) {
      if (evt === "click") listeners.push(fn);
    },
  };
  return { button, click: () => listeners.map((fn) => fn()) };
}

const add = makeAddButton();
globalThis.window = globalThis;
globalThis.location = { search: "?entries_sort=sort_time" };
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.setTimeout = (fn) => {
  captured.timers.push(fn);
  return captured.timers.length;
};
globalThis.htmx = {
  ajax(method, url, options) {
    captured.ajax.push({ method, url, options });
  },
  process() {},
};
globalThis.HTMLElement = class HTMLElement {};
globalThis.HTMLFormElement = class HTMLFormElement {};
globalThis.Node = class Node {};
globalThis.fetch = async (url, options) => {
  captured.fetches.push({ url, options });
  return { ok: true, json: async () => ({ ok: true, duplicated: captured.fetches.length > 1 }) };
};
const laterhubForm = {
  querySelector: () => null,
};
globalThis.document = {
  getElementById(id) {
    if (id === "laterhub-panel") return { querySelector: () => laterhubForm };
    return null;
  },
  querySelector() {
    return null;
  },
  querySelectorAll(sel) {
    return sel === "[data-laterhub-add]" ? [add.button] : [];
  },
  createElement: () => ({ name: "" }),
  addEventListener(evt, fn) {
    if (evt === "DOMContentLoaded") captured.domReady = fn;
  },
  body: {
    addEventListener(evt, fn) {
      if (evt === "htmx:afterSwap") captured.afterSwap.push(fn);
    },
  },
};

new Function(source)();
captured.domReady();

const flushTimers = () => captured.timers.splice(0).forEach((fn) => fn());

await add.click()[0];
assert(captured.fetches.length === 1, "点一次:只发一个请求");
assert(captured.fetches[0].url === "/actions/laterhub/add-link", "请求打到收链接口");
const payload = JSON.parse(captured.fetches[0].options.body);
assert(payload.url === "/markdown/alphapai/a.md" && payload.title === "全球蓝宝书", "请求体带原条目的链接与标题");
assert(add.button.textContent === "已加入", "首次收录:按钮反馈「已加入」");
assert(captured.ajax.length === 1, "加入后刷新稍后处理面板");
assert(captured.ajax[0].url.startsWith("/fragments/laterhub"), "刷新走稍后处理片段路由");
assert(captured.ajax[0].options.target === "#laterhub-panel", "刷新目标为右栏面板");

flushTimers();
assert(add.button.textContent === "加入" && add.button.disabled === false, "反馈到点后复原按钮");

await add.click()[0];
assert(add.button.textContent === "已在", "重复收录:反馈「已在」");

if (failed) {
  console.error(`\n${failed} 项失败`);
  process.exit(1);
}
console.log("\n全部通过");
