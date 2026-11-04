// 稍后看翻页自检:直接从 api.py 抽出页内 JS,用假 fetch 驱动。
// 核心回归:只有确认拿到完整列表(complete=true)才允许按「源内已移除」判定完成;
// pn 被忽略、count 没拿够、撞页数上限都必须判为不完整。
// 运行: node tests/test_laterhub_watchlater_paging_node.mjs
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const pySource = readFileSync(join(here, "..", "connectors", "bilibili", "api.py"), "utf8");
const matched = pySource.match(/_BILIBILI_WATCHLATER_PAGE_JS = r"""([\s\S]*?)"""/);
if (!matched) {
  console.error("FAIL 未能从 api.py 提取稍后看页内 JS");
  process.exit(1);
}
const pageFn = new Function(`return (${matched[1].trim()})`)();

let failed = 0;
function assert(cond, label) {
  if (cond) {
    console.log(`PASS ${label}`);
  } else {
    failed += 1;
    console.error(`FAIL ${label}`);
  }
}

const makeItem = (index) => ({ bvid: `BV${index}`, title: `视频 ${index}` });

// pages: pn => 接口载荷;count: data.count 或缺省
function drive({ pages, count, maxPages = 50 }) {
  const requested = [];
  globalThis.fetch = async (url) => {
    const pn = Number(new URL(url).searchParams.get("pn"));
    requested.push(pn);
    const list = pages(pn);
    const data = { list };
    if (count !== undefined) data.count = count;
    return { json: async () => ({ code: 0, message: "0", data }) };
  };
  const args = JSON.stringify({
    url: "https://api.bilibili.com/x/v2/history/toview/web",
    ps: 20,
    maxPages,
    requestTimeoutMs: 20000,
  });
  return pageFn(args).then((payload) => ({ payload, requested }));
}

async function main() {
  {
    // 20/20/5 三页,count=45:拿满且翻完 -> 完整
    const { payload, requested } = await drive({
      count: 45,
      pages: (pn) => {
        const start = (pn - 1) * 20;
        return Array.from({ length: Math.max(0, Math.min(20, 45 - start)) }, (_, i) => makeItem(start + i));
      },
    });
    assert(payload.complete === true, "三页 20/20/5 判为完整");
    assert(payload.items.length === 45, "45 条全部收集");
    assert(requested.length === 3, "恰好请求 3 页");
  }

  {
    // pn 被忽略(每页都返回第一页),count 未满:整页零新增必须判不完整,且不得翻满上限
    const { payload, requested } = await drive({
      count: 45,
      pages: () => Array.from({ length: 20 }, (_, i) => makeItem(i)),
    });
    assert(payload.complete === false, "pn 被忽略时判为不完整");
    assert(requested.length === 2, "零新增即停,不翻满上限");
  }

  {
    // 撞 maxPages 仍每页满:不完整
    const { payload } = await drive({
      count: 100,
      maxPages: 2,
      pages: (pn) => Array.from({ length: 20 }, (_, i) => makeItem((pn - 1) * 20 + i)),
    });
    assert(payload.complete === false, "撞页数上限判为不完整");
    assert(payload.items.length === 40, "只收集已请求到的 40 条");
  }

  {
    // 接口不报 count,短页即到底 -> 完整
    const { payload } = await drive({
      pages: () => [makeItem(0), makeItem(1)],
    });
    assert(payload.complete === true, "count 缺失时短页判为完整");
  }

  {
    // 短页但 count 还没拿够(接口自相矛盾):保守判不完整
    const { payload } = await drive({
      count: 45,
      pages: () => [makeItem(0), makeItem(1)],
    });
    assert(payload.complete === false, "短页但 count 未满判为不完整");
  }

  {
    // 空列表 + count=0:合法清空,判为完整
    const { payload } = await drive({ count: 0, pages: () => [] });
    assert(payload.complete === true && payload.items.length === 0, "空稍后看判为完整空批次");
  }

  {
    // 非零 code 原样带回,由 Python 侧抛错
    globalThis.fetch = async () => ({ json: async () => ({ code: -101, message: "账号未登录" }) });
    const payload = await pageFn(
      JSON.stringify({
        url: "https://api.bilibili.com/x/v2/history/toview/web",
        ps: 20,
        maxPages: 50,
        requestTimeoutMs: 20000,
      })
    );
    assert(payload.code === -101, "非零 code 带回 Python 侧处理");
  }

  if (failed) {
    console.error(`\n${failed} 个断言失败`);
    process.exit(1);
  }
  console.log("\n全部通过");
}

main();
