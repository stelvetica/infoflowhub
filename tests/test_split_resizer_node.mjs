// 中缝拖拽自检:最小 DOM stub 加载 app.js,驱动 pointer/keyboard 事件验证比例计算与持久化。
// 运行: node tests/test_split_resizer_node.mjs
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, "..", "web", "static", "app.js"), "utf8");

const SHELL_WIDTH = 1000; // 左栏 + 间隔 + 右栏
const RATIO_KEY = "infoflowhub:laterhub-ratio";
const LEGACY_KEY = "infoflowhub:laterhub-width";

let failed = 0;
function assert(cond, label) {
  if (cond) {
    console.log(`PASS ${label}`);
  } else {
    failed += 1;
    console.error(`FAIL ${label}`);
  }
}

function makeStyle() {
  const props = {};
  return {
    setProperty(name, value) {
      props[name] = value;
    },
    getPropertyValue(name) {
      return props[name] ?? "";
    },
  };
}

function makeElement() {
  const listeners = new Map();
  const attrs = new Map();
  const classes = new Set();
  return {
    dataset: {},
    style: makeStyle(),
    classList: {
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
    },
    addEventListener(evt, fn) {
      listeners.set(evt, fn);
    },
    fire(evt, payload) {
      const fn = listeners.get(evt);
      if (fn) fn(payload);
    },
    hasListener: (evt) => listeners.has(evt),
    setAttribute(name, value) {
      attrs.set(name, value);
    },
    getAttribute: (name) => attrs.get(name) ?? null,
    setPointerCapture() {},
    releasePointerCapture() {},
    hasPointerCapture: () => true,
  };
}

const store = new Map();

// 每次「打开页面」都换一套全新 DOM 元素(与真实刷新一致),localStorage 保持不丢。
function loadPage() {
  const captured = { domReady: null, windowListeners: new Map() };
  const resizer = makeElement();
  const shell = makeElement();
  const side = makeElement();

  shell.clientWidth = SHELL_WIDTH;
  shell.getBoundingClientRect = () => ({ left: 0, right: SHELL_WIDTH, width: SHELL_WIDTH });
  shell.querySelector = (selector) => (selector === ".split-pane-side" ? side : null);
  // 右栏实际宽度跟随已应用的比例,键盘微调以此为基准
  side.getBoundingClientRect = () => {
    const ratio = parseFloat(shell.style.getPropertyValue("--laterhub-width")) / 100;
    return { width: (Number.isFinite(ratio) ? ratio : 0.36) * SHELL_WIDTH };
  };

  globalThis.window = globalThis;
  globalThis.localStorage = {
    getItem: (key) => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => store.set(key, String(value)),
    removeItem: (key) => store.delete(key),
  };
  globalThis.getComputedStyle = () => ({ getPropertyValue: (name) => (name === "--laterhub-min" ? "320px" : "") });
  globalThis.document = {
    querySelector(selector) {
      if (selector === "[data-split-resizer]") return resizer;
      if (selector === ".split-shell") return shell;
      return null;
    },
    querySelectorAll: () => [],
    getElementById: () => null,
    createElement: () => makeElement(),
    addEventListener(evt, fn) {
      if (evt === "DOMContentLoaded") captured.domReady = fn;
    },
    body: { addEventListener() {}, style: {} },
    cookie: "",
  };
  globalThis.window.addEventListener = (evt, fn) => captured.windowListeners.set(evt, fn);
  globalThis.htmx = { ajax() {} };

  new Function(source)();
  captured.domReady();

  return {
    resizer,
    shell,
    appliedRatio: () => shell.style.getPropertyValue("--laterhub-width"),
    pointer(evt, clientX) {
      const fn = captured.windowListeners.get(evt);
      if (fn) fn({ pointerId: 1, clientX });
    },
    drag(from, to) {
      resizer.fire("pointerdown", { pointerId: 1, clientX: from, preventDefault() {} });
      this.pointer("pointermove", to);
      this.pointer("pointerup", to);
    },
    key(name) {
      resizer.fire("keydown", { key: name, preventDefault() {} });
    },
  };
}

// 用例1: 没有存储时不写内联变量,沿用 CSS 默认比例
const first = loadPage();
assert(first.appliedRatio() === "", "无存储时不覆盖 CSS 默认比例");
assert(store.has(RATIO_KEY) === false, "无存储时不会凭空写入比例");

// 用例2: 拖到 50% 处松手 → 立即应用并落盘
first.drag(640, 500);
assert(first.appliedRatio() === "50.00%", "拖动即时改比例");
assert(store.get(RATIO_KEY) === "0.5", "松手即存比例");
assert(first.resizer.getAttribute("aria-valuenow") === "50", "无障碍值同步为百分比");

// 用例3: 拖到最右 → 夹在下限(CSS --laterhub-min 320px)
first.drag(500, 990);
assert(first.appliedRatio() === "32.00%", "拖到最右时右栏不小于 320px");

// 用例4: 拖到最左 → 夹在上限 60%
first.drag(990, 60);
assert(first.appliedRatio() === "60.00%", "拖到最左时右栏不超过 60%");

// 用例5: 键盘微调 ±2%,同样即调即存
first.drag(60, 500); // 回到 50%
first.key("ArrowLeft");
assert(first.appliedRatio() === "52.00%", "方向键左移加宽右栏 2%");
assert(store.get(RATIO_KEY) === "0.52", "键盘微调同样落盘");
first.key("ArrowRight");
assert(first.appliedRatio() === "50.00%", "方向键右移收窄右栏 2%");

// 用例6: 刷新后沿用已存比例,且旧像素宽度键被清掉
store.set(LEGACY_KEY, "460");
const reloaded = loadPage();
assert(reloaded.appliedRatio() === "50.00%", "刷新后沿用已存比例");
assert(store.has(LEGACY_KEY) === false, "旧的像素宽度键被清理");

if (failed > 0) {
  console.error(`\n${failed} 项断言失败`);
  process.exit(1);
}
console.log("\n中缝拖拽逻辑自检全部通过");