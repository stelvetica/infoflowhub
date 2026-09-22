// 表格续载自检:最小 DOM stub 加载 app.js,验证「填满即止、滚到底续载、取完停手、失败可重试」。
// 运行: node tests/test_autoload_node.mjs
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, "..", "web", "static", "app.js"), "utf8");

const ROWS_PER_PAGE = 5;
const ROW_HEIGHT = 25;

let failed = 0;
function assert(cond, label) {
  if (cond) {
    console.log(`PASS ${label}`);
  } else {
    failed += 1;
    console.error(`FAIL ${label}`);
  }
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 20));

// 取 URL 里的续载游标(URLSearchParams 会做百分号解码)
const afterCursor = (url) => new URLSearchParams(url.split("?")[1]).get("entries_after");

function makeElement(extra = {}) {
  const listeners = new Map();
  return {
    dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    style: {},
    addEventListener(evt, fn) {
      listeners.set(evt, fn);
    },
    fire(evt, payload) {
      const fn = listeners.get(evt);
      if (fn) fn(payload);
    },
    setAttribute() {},
    getAttribute: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    ...extra,
  };
}

function fakeInput(name, value = "") {
  return { type: "hidden", name, value };
}

function makeForm(fields) {
  return {
    dataset: {},
    _fields: fields,
    appendChild(el) {
      fields.push(el);
    },
    querySelector(selector) {
      const match = /name="([^"]+)"/.exec(selector || "");
      return match ? fields.find((el) => el.name === match[1]) || null : null;
    },
    addEventListener() {},
  };
}

// 一次「打开页面」:全新 DOM;表格长度按已加载页数推算,越加越长
function loadPage({ totalPages, wrapHeight, preloadedPages = 1, failFrom = null }) {
  const fetched = [];
  const processed = [];
  const tbody = makeElement();
  let loadedPages = preloadedPages;
  const rows = () => tbody.innerHTML.split("<tr").length - 1;
  tbody.querySelectorAll = (selector) =>
    selector === "tr" ? Array.from({ length: rows() }, () => makeElement()) : [];
  tbody.insertAdjacentHTML = (position, html) => {
    if (position === "beforeend") tbody.innerHTML += html;
  };
  // children 按行数实时生成,用于验证「只把新追加的行交给 HTMX process」
  Object.defineProperty(tbody, "children", {
    get: () => Array.from({ length: rows() }, (_, index) => ({ __row: index })),
  });
  tbody.innerHTML = Array.from({ length: preloadedPages * ROWS_PER_PAGE }, () => "<tr><td>条目</td></tr>").join("");

  const wrap = makeElement({ scrollTop: 0, clientHeight: wrapHeight });
  Object.defineProperty(wrap, "scrollHeight", { get: () => loadedPages * ROWS_PER_PAGE * ROW_HEIGHT });
  const count = makeElement({ textContent: `已加载 ${preloadedPages * ROWS_PER_PAGE} / 共 ${totalPages * ROWS_PER_PAGE} 条` });
  const meta = makeElement({
    dataset: {
      nextCursor: preloadedPages < totalPages ? `cursor~${preloadedPages}` : "",
      total: String(totalPages * ROWS_PER_PAGE),
    },
  });
  const form = makeForm([fakeInput("entries_sort", "sort_time"), fakeInput("entries_dir", "desc")]);

  globalThis.window = globalThis;
  globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
  globalThis.getComputedStyle = () => ({ getPropertyValue: () => "" });
  globalThis.htmx = { ajax() {}, process: (node) => processed.push(node) };
  globalThis.FormData = class {
    constructor(target) {
      this._fields = target._fields;
    }
    forEach(cb) {
      this._fields.forEach((el) => cb(el.value, el.name));
    }
  };
  globalThis.DOMParser = class {
    parseFromString(html) {
      const body = /<tbody[^>]*>([\s\S]*?)<\/tbody>/.exec(html)?.[1] ?? "";
      const cursor = /data-next-cursor="([^"]*)"/.exec(html)?.[1] ?? "";
      const total = /data-total="([^"]*)"/.exec(html)?.[1] ?? "";
      return {
        querySelector: (selector) => {
          if (selector === "tbody") return { innerHTML: body };
          if (selector === "[data-entries-fragment]") return { dataset: { nextCursor: cursor, total } };
          return null;
        },
      };
    }
  };
  globalThis.fetch = (url) => {
    fetched.push(url);
    const cursor = new URLSearchParams(url.split("?")[1]).get("entries_after") || "";
    const page = Number(cursor.split("~")[1] || 1) + 1;
    if (failFrom !== null && page >= failFrom) {
      return Promise.resolve({ ok: false, status: 500 });
    }
    loadedPages = Math.max(loadedPages, page);
    const rows = Array.from({ length: ROWS_PER_PAGE }, () => "<tr><td>条目</td></tr>").join("");
    const nextCursor = page < totalPages ? `cursor~${page}` : "";
    return Promise.resolve({
      ok: true,
      text: () => Promise.resolve(`<div data-entries-fragment data-next-cursor="${nextCursor}" data-total="${totalPages * ROWS_PER_PAGE}"><tbody>${rows}</tbody></div>`),
    });
  };
  globalThis.document = {
    querySelector(selector) {
      if (selector === "[data-entries-fragment]") return meta;
      if (selector === "#entries-panel .table-wrap") return wrap;
      if (selector === "#entries-panel tbody") return tbody;
      if (selector === "#entries-panel .pagination .subtle") return count;
      if (selector === ".entries-header-form") return form;
      return null;
    },
    querySelectorAll: () => [],
    getElementById: () => null,
    createElement: () => fakeInput(""),
    addEventListener(evt, fn) {
      if (evt === "DOMContentLoaded") globalThis.__domReady = fn;
    },
    body: { addEventListener() {}, style: {} },
    cookie: "",
  };

  new Function(source)();
  globalThis.__domReady();

  return { fetched, processed, wrap, tbody, count, rows, scrollTo: (top) => { wrap.scrollTop = top; wrap.fire("scroll"); } };
}

// 用例1: 首屏没填满 → 自动续到填满为止(300px 区:1 页 125px 不满,2 页 250px 不满,3 页 375px 才满)
{
  const page = loadPage({ totalPages: 5, wrapHeight: 300 });
  await settle();
  assert(page.fetched.length === 2, "未填满时自动追加下一页(共 2 次)");
  assert(afterCursor(page.fetched[0]) === "cursor~1", "首次续载带上游标");
  assert(afterCursor(page.fetched[1]) === "cursor~2", "仍未填满则按新游标继续取");
  assert(page.rows() === 3 * ROWS_PER_PAGE, "追加的行进入表格");
  assert(page.count.textContent === "已加载 15 / 共 25 条", "计数行改为已加载条数");
  // 追加的行必须交给 HTMX process,否则行内 hx-post(「完成」按钮)是死的
  assert(page.processed.length === 2 * ROWS_PER_PAGE, "只把新追加的行交给 HTMX process");
  assert(page.processed.every((node) => node && node.__row !== undefined), "process 收到的确实是追加的行");
}

// 用例2: 已填满则不预取;滚到接近底部才续载,顶部滚动不触发
{
  const page = loadPage({ totalPages: 8, wrapHeight: 300, preloadedPages: 4 });
  await settle();
  assert(page.fetched.length === 0, "首屏已填满时不多取");
  page.scrollTo(0);
  await settle();
  assert(page.fetched.length === 0, "滚到顶部不触发续载");
  page.scrollTo(200); // 500-300=200 的可滚区间,离底部 <160px
  await settle();
  assert(page.fetched.length === 1 && afterCursor(page.fetched[0]) === "cursor~4", "滚到接近底部按游标续载");
  assert(page.rows() === 5 * ROWS_PER_PAGE, "续载行追加在末尾");
}

// 用例3: 取到最后一页即停手
{
  const page = loadPage({ totalPages: 4, wrapHeight: 300, preloadedPages: 4 });
  await settle();
  page.scrollTo(200);
  await settle();
  page.scrollTo(200);
  await settle();
  assert(page.fetched.length === 0, "没有下一游标后不再请求");
}

// 用例4: 同一时刻只发一个请求(并发去重)
{
  const page = loadPage({ totalPages: 8, wrapHeight: 300, preloadedPages: 4 });
  await settle();
  page.scrollTo(200);
  page.scrollTo(200);
  await settle();
  assert(page.fetched.length === 1, "连续滚动事件不重复发请求");
}

// 用例5: 请求失败给出可重试提示,再次滚动会重试
{
  const page = loadPage({ totalPages: 8, wrapHeight: 300, preloadedPages: 4, failFrom: 5 });
  await settle();
  page.scrollTo(200);
  await settle();
  assert(page.count.textContent === "加载失败，继续滚动重试", "失败时计数行提示可重试");
  page.scrollTo(200);
  await settle();
  assert(page.fetched.length === 2, "再次滚动会重试");
}

if (failed > 0) {
  console.error(`\n${failed} 项断言失败`);
  process.exit(1);
}
console.log("\n表格续载逻辑自检全部通过");