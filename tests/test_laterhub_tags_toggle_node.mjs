// 标签面板收展状态自检:无框架、最小 DOM stub,直接加载 app.js 驱动点击与片段交换。
// 核心回归:点掉已选中的标签回到「全部标签」时,片段刷新后**不得**自动收起面板。
// 运行: node tests/test_laterhub_tags_toggle_node.mjs
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

const captured = { domReady: null, afterSwap: [], submits: 0 };

function makeClassList() {
  const set = new Set();
  return {
    add: (c) => set.add(c),
    remove: (c) => set.delete(c),
    contains: (c) => set.has(c),
    toggle: (c, on) => (on === undefined ? (set.has(c) ? set.delete(c) : set.add(c)) : on ? set.add(c) : set.delete(c)),
  };
}

// 模拟服务端渲染的标签面板:filtered 决定初始展开态与隐藏域里的筛选值
function makeTagsDom({ filtered = "" } = {}) {
  const filterInput = { name: "laterhub_filter_tag", value: filtered };
  const form = {
    querySelector(sel) {
      return sel === 'input[name="laterhub_filter_tag"]' ? filterInput : null;
    },
    requestSubmit() {
      captured.submits += 1;
    },
  };
  const clicks = [];
  const btn = {
    dataset: {},
    attrs: {},
    classList: makeClassList(),
    setAttribute(k, v) {
      btn.attrs[k] = v;
    },
    addEventListener(evt, fn) {
      if (evt === "click") clicks.push(fn);
    },
  };
  const chips = {
    hidden: !filtered,
    closest(sel) {
      return sel === "form" ? form : null;
    },
  };
  return {
    btn,
    chips,
    filterInput,
    click: () => clicks.forEach((fn) => fn()),
  };
}

let dom = makeTagsDom({ filtered: "" });
const panel = { dataset: {}, querySelectorAll: () => [] };

globalThis.window = globalThis;
globalThis.localStorage = { getItem: () => null, setItem() {} };
globalThis.htmx = { ajax() {} };
// app.js 的焦点/模态逻辑会做 instanceof HTMLElement 判定
globalThis.HTMLElement = class HTMLElement {};
globalThis.Node = class Node {};
globalThis.document = {
  getElementById(id) {
    if (id === "laterhub-chips") return dom.chips;
    if (id === "laterhub-panel") return panel;
    return null;
  },
  querySelector(sel) {
    if (sel === ".laterhub-tags-toggle-btn") return dom.btn;
    return null;
  },
  querySelectorAll() {
    return [];
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

// 模拟一次片段交换:换上新 DOM 并触发 afterSwap
function swap(next) {
  dom = next;
  captured.afterSwap.forEach((fn) => fn({}));
}

assert(dom.chips.hidden === true, "页面加载无筛选:面板收起(以服务端渲染为准)");

dom.click();
assert(dom.chips.hidden === false && dom.btn.attrs["aria-expanded"] === "true", "点缩放按钮:展开");

swap(makeTagsDom({ filtered: "" }));
assert(dom.chips.hidden === false, "交换后(服务端按无筛选渲染成收起):保持展开");

// 用户报告的场景 A:点「美食」筛选,再点一次「美食」取消 →
// chip 是表单提交按钮,由 htmx 片段交换重建面板,不经过收展按钮的 JS。
// 服务端只能按「无筛选」渲染成收起,面板必须靠展开意图保持展开,不得自动收缩。
swap(makeTagsDom({ filtered: "生活_美食" }));
assert(dom.chips.hidden === false, "选中美食后:片段仍展开");
swap(makeTagsDom({ filtered: "" }));
assert(dom.chips.hidden === false, "再点一次美食回到全部标签:面板**不收缩**");
assert(dom.btn.attrs["aria-expanded"] === "true", "回到全部标签:缩放按钮仍标记为展开");

// 场景 B:点缩放按钮 = 收起并清空筛选(与点掉 chip 是两件事)
swap(makeTagsDom({ filtered: "生活_美食" }));
dom.click();
assert(captured.submits === 1 && dom.filterInput.value === "", "缩放按钮:清空隐藏域并请求片段刷新");
swap(makeTagsDom({ filtered: "" }));
assert(dom.chips.hidden === true, "缩放按钮收起后:保持收起");

// 场景 C:无筛选时点缩放按钮只收起,不发请求
dom.click();
assert(dom.chips.hidden === false, "收起态点缩放按钮:展开");
dom.click();
assert(dom.chips.hidden === true && dom.btn.attrs["aria-expanded"] === "false", "展开态点缩放按钮:收起");
assert(captured.submits === 1, "无筛选时收起不发请求");
swap(makeTagsDom({ filtered: "" }));
assert(dom.chips.hidden === true, "主动收起后交换:保持收起");

if (failed) {
  console.error(`\n${failed} 项失败`);
  process.exit(1);
}
console.log("\n全部通过");
