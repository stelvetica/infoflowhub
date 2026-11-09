---
name: market-hotspot
description: 生成 A 股近 3 个交易日「市场热点」报告：申万二级/一级行业涨幅排名、Top 5 行业的强势股与广度，并留下 AI 原因分析占位。触发条件：用户要求「市场热点/行业涨幅排行/近3日强势板块」时。
---

# 市场热点分析

两段式：脚本产出确定性事实（MD 表格 + JSON 底稿）→ AI 按 `references/reason-template.md` 补全 Top 5 原因。

## 路径约定

| 用途 | 路径 |
|---|---|
| 脚本 | `skills/market-hotspot/scripts/collect_hotspot.py`（skill 目录内） |
| 产物根（主 checkout，不是 worktree） | `C:\Users\ETT\Playground\infoflowhub\data\reports\` |
| 报告 | `<产物根>\市场热点_YYYY-MM-DD.md` |
| 底稿 | `<产物根>\市场热点底稿_YYYY-MM-DD.json` |
| 科创板行业缓存 | `<产物根>\.cache\kcb_industry.json`（TTL 7 天） |
| 行业覆盖 | `references/kcb_industry_overrides.json`（`{"sh688xxx": "申万二级名"}`） |

## 运行

```bash
C:\Users\ETT\Playground\infoflowhub\.venv\Scripts\python.exe "<skill_dir>\scripts\collect_hotspot.py"
```

| 参数 | 行为 |
|---|---|
| 无参 | 抓取 + 计算 + 写 MD/JSON/科创板缓存；部分成功退出 0，致命退出 2 |
| `--dry-run` | 抓取 + 计算 + 打印摘要，**不写任何文件**（含缓存） |
| `--selftest` | 纯函数自检，不触网不落盘；PASS 0 / FAIL 1 |

不提供 `--date/--top/--workers/--out-dir`。

## 口径

- 窗口：上证指数日 K 最后 4 根 D0..D3；运行时间早于 15:05 时剔除当日盘中 bar，15:05 后含当日。
- 近 3 天 = D1..D3；`r3 = close[D3]/close[D0]-1`；基准 = 上证指数；超额 = 行业 r3 - 基准 r3。
- 行业：申万二级 124 个（主表）+ 申万一级 31 个（对照），按 r3 降序；并列按板块近 3 日日均成交额降序、代码升序，各取前 5。
- 股票范围：沪深 A（含主板/创业板/科创板），剔 ST/退市/停牌/上市不足 60 根有效 bar，近 3 日（D1..D3）日均成交额 < 5000 万元，剔北交所与 B 股。
- 强势股：`r3 > r3_bench`（严格大于，不要求绝对为正）。MD 每行业列前 5 只，JSON 存全部。
- 广度（仅 Top 5）：中位数、强势占比、权重拉动（中位数 < 行业 r3 - 0.5pp）、广度不足（<30%）、无强势股；只提示，不改排名。
- 成交额优先取腾讯 K 线第 8 列（万元）；缺失才按 `volume×close` 估算并标 `amount_source=estimated`（688/689 volume 为股，其余为手）。

## 数据源

| 数据 | 接口 |
|---|---|
| 行业目录 | 腾讯 `getRank?board_type=hy2|hy` |
| 板块/个股/指数日 K | 腾讯 `newfqkline`（`qfqday`，个股 70 根） |
| 行业成分股 | 腾讯 `getBoardRankList`（分页步长 200，不含科创板） |
| 科创板代码 | 新浪 `Market_Center.getHQNodeData?node=kcb`（7 页） |
| 科创板行业 | 雪球 `f10/cn/industry.json`（先 GET `https://www.xueqiu.com/` 取 cookie；400016 重建会话重试一次） |

- **禁止**任何东财（eastmoney / push2 / push2his）调用；源码不得出现相关字样。
- 雪球行业名是 SW2014 风格：精确匹配 → 补 `Ⅱ` 后缀（如 IT服务→IT服务Ⅱ）→ 无歧义改名（塑料制品→塑料、汽车服务及其他→汽车服务）→ 按 code 覆盖 → 未归类（如军工装备歧义）。

## 失败降级

| 场景 | 行为 |
|---|---|
| 指数 / 申万二级目录 / 窗口失败 | 退出 2，不覆盖旧文件 |
| 申万一级失败 | 照出二级，数据质量段注明 |
| 单板块 / 单股失败 | 剔除并计数，不阻塞 |
| 有效二级 < 5 | 用现有数量并在结论注明；= 0 退出 2 |
| 科创板代码失败 | 不纳入科创板成分，注明 |
| 雪球行业失败 | 过期缓存 → 未归类，注明 |
| 写文件被占用 | 重试 1 次，仍失败退出 2，旧文件保留 |

## 阶段一：脚本

脚本输出 `市场热点_YYYY-MM-DD.md`（AI 原因占位 `<!-- AI 填写 -->`）与 `市场热点底稿_YYYY-MM-DD.json`（事实底稿，不含 AI 原因）。文件名日期 = 生成日，数据截止日写在报告头与 JSON `window.end_date`。

## 阶段二：AI 原因分析

读 `references/reason-template.md`，按其中证据顺序补全 `## 原因分析（申万二级 Top 5）` 下 5 个 `###` 小节的 `- 原因：` 与 `- 依据：`。不修改表格、数字、口径与数据质量段，不新增行业小节。

## 校验

- `--selftest` 退出 0。
- `--dry-run` 的窗口应为最近 3 个完整交易日，Top 5 行业与强势股数量合理。
- 正式运行后 MD 含申万二级 Top 5 表、5 个原因小节、一级对照与全量排名 `<details>`；JSON 计数恒等式成立且无 NaN/Infinity。
