#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""近期操作分析：成交 CSV + 估值表 → 近一周/近一月 的行业与个股加减、仓位变化。

只做确定性抽取与汇总；「为什么动、意味着什么」留给 AI（见 SKILL.md 阶段二）。

口径（与 SKILL.md 一致，改口径先改这里）：
- 窗口按**交易日**数：近一周 = 最近 5 个交易日，近一月 = 最近 20 个交易日。
  交易日取自成交 CSV 自身的日期集合（该数据源每日落盘，无需另建日历）。
- 权益敞口 = 股票市值占净值% + 收益互换保证金占净值%。
  互换底层均为股票，按股票计入；仅臻享1期存在互换。
- 净值与仓位只取最新一日估值表；加减一律换算为「占最新净值%」。
- 回购（GC001 等）不计入股票加减，单独统计。
- 行业表内置在 config 的 industry_overrides（code → [名称, 行业]），不依赖标的信息库；
  新标的出现时归「未归类」，由 AI 查证后补进 config。

产物（写 config.output_dir）：
- 操作分析_YYYY-MM-DD.md   解读（个股/行业/特征/产品分化由脚本，各产品点评由 AI）→ 口径 → 总览 → 仓位 → 明细
- 操作底稿_YYYY-MM-DD.json 结构化底稿（AI 读取用）
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import glob
import io
import json
import sys
from collections import defaultdict
from pathlib import Path

try:
    import openpyxl
except ImportError:  # 读 .xlsx 需要；只跑 --selftest 时可不装
    openpyxl = None
try:
    import xlrd
except ImportError:  # 读 .xls 需要
    xlrd = None

BASE_DIR = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = BASE_DIR / "config" / "portfolio_operations.json"
BUY, SELL = "买入", "卖出"
REPO = "回购"
UNKNOWN = "未归类"
EQUITY_PREFIX = "以公允价值计量且其变动计入当期损益的股票投资"


# ---------- 基础 ----------

def load_config(path: Path) -> dict:
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    for key in ("trades_csv", "valuation_root", "output_dir"):
        if key not in cfg:
            raise SystemExit(f"配置缺字段 {key}: {path}")
    cfg.setdefault("top_n", 8)
    cfg.setdefault("windows", {"week": 5, "month": 20})
    cfg.setdefault("industry_overrides", {})
    for p in cfg["products"]:
        p.setdefault("valuation_dir", p["position_dir"])
    return cfg


def load_trades(path: str, products: set[str]) -> list[dict]:
    """只读观察产品的买卖记录：CSV 是公司全量流水（30 万行），先过滤再聚合。"""
    rows = []
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
        for r in csv.DictReader(f):
            if r.get("委托方向") not in (BUY, SELL) or r.get("产品名称") not in products:
                continue
            try:
                amt = float(r.get("成交金额") or 0)
            except ValueError:
                continue
            rows.append(
                {
                    "date": r["成交日期"],
                    "prod": r["产品名称"],
                    "code": r["证券代码"],
                    "name": r.get("证券名称") or "",
                    "side": r["委托方向"],
                    "amt": amt,
                }
            )
    if not rows:
        raise SystemExit(f"成交 CSV 没有可用记录: {path}")
    return rows


def resolve(code: str, overrides: dict) -> tuple[str, str]:
    """返回 (名称, 行业一级)。"""
    if code in overrides:
        name, industry = overrides[code]
        return name, industry.split("-")[0]
    # 成交表里部分行的证券名称是席位号，别当名字用
    return code, UNKNOWN


def build_windows(dates: list[str], end: str, windows: dict) -> dict[str, tuple[str, str]]:
    ds = sorted({d for d in dates if d <= end})
    if not ds:
        raise SystemExit(f"报告日 {end} 之前没有任何成交记录")
    out = {}
    for label, n in windows.items():
        start = ds[-n] if len(ds) >= n else ds[0]
        out[label] = (start, end)
    return out


def aggregate(trades: list[dict], csv_names: set[str], start: str, end: str,
              overrides: dict) -> dict:
    picked = [t for t in trades if t["prod"] in csv_names and start <= t["date"] <= end]
    stocks: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    repo_amt = 0.0
    buy = sell = 0.0
    for t in picked:
        _, industry = resolve(t["code"], overrides)
        if industry == REPO:
            repo_amt += t["amt"]
            continue
        if t["side"] == BUY:
            buy += t["amt"]
            stocks[t["code"]][0] += t["amt"]
        else:
            sell += t["amt"]
            stocks[t["code"]][1] += t["amt"]
    industries: dict[str, float] = defaultdict(float)
    items = []
    for code, (b, s) in stocks.items():
        name, industry = resolve(code, overrides)
        net = b - s
        industries[industry] += net
        items.append({"code": code, "name": name, "industry": industry,
                      "buy": round(b), "sell": round(s), "net": round(net)})
    items.sort(key=lambda x: -x["net"])
    return {
        "start": start,
        "end": end,
        "buy": round(buy),
        "sell": round(sell),
        "net": round(buy - sell),
        "repo": round(repo_amt),
        "industries": {k: round(v) for k, v in sorted(industries.items(), key=lambda x: -x[1])},
        "stocks": items,
        "unresolved": sorted({i["code"] for i in items if i["industry"] == UNKNOWN}),
    }


# ---------- 估值表 ----------

def _read_rows(path: Path) -> list[list]:
    if path.suffix.lower() == ".xlsx":
        if openpyxl is None:
            raise SystemExit("读 .xlsx 需要 openpyxl")
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        rows = [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
        wb.close()
        return rows
    if xlrd is None:
        raise SystemExit("读 .xls 需要 xlrd")
    # xlrd 对部分券商导出的 .xls 会打印 OLE2 告警，属噪音，不打断运行
    with contextlib.redirect_stderr(io.StringIO()):
        sh = xlrd.open_workbook(str(path)).sheets()[0]
    return [[c.value for c in sh.row(i)] for i in range(sh.nrows)]


def _num(row: list, idx: int) -> float | None:
    try:
        return float(row[idx])
    except (TypeError, ValueError, IndexError):
        return None


def parse_valuation(path: Path) -> dict:
    return parse_valuation_rows(_read_rows(path), str(path))


def parse_valuation_rows(rows: list[list], source: str = "<rows>") -> dict:
    # 部分模板有 2–3 行重复表头，只有最后一行带「市值」列
    hdr_i = next((i for i, r in enumerate(rows)
                  if r and str(r[0]).strip() == "科目代码"
                  and any(str(c).strip() == "市值" for c in r)), None)
    if hdr_i is None:
        hdr_i = next((i for i, r in enumerate(rows) if r and str(r[0]).strip() == "科目代码"), None)
    if hdr_i is None:
        raise SystemExit(f"估值表结构不认识（找不到表头）: {source}")
    hdr = [str(c).strip() if c is not None else "" for c in rows[hdr_i]]
    # 部分管理人模板有「原币/本币」两列同名市值，取最后一列（本币）；占比列名也分两种
    col_mv = max(i for i, h in enumerate(hdr) if h == "市值")
    col_mvpct = next((i for i, h in enumerate(hdr) if h in ("市值占净值%", "市值占比")), None)
    # 「市值占比」是 0–1 小数，「市值占净值%」已是百分数
    pct_scale = 1.0 if col_mvpct is not None and "%" in hdr[col_mvpct] else 100.0

    def pct(row: list) -> float | None:
        v = _num(row, col_mvpct) if col_mvpct is not None else None
        return None if v is None else v * pct_scale

    out = {"nav": None, "stock_pct": None, "stock_mv": 0.0,
           "fund_pct": None, "fund_mv": 0.0, "swap_pct": 0.0, "date": ""}
    # 估值日期在表头上方（第 2 行），不在明细区；两种写法：「估值日期」「日期」
    for r in rows[:hdr_i]:
        head = str(r[0] or "") if r else ""
        if ("估值日期" in head or head.strip().startswith("日期")) and "：" in head:
            out["date"] = head.split("：", 1)[-1].strip()[:10].replace("-", "")
    for r in rows[hdr_i + 1:]:
        if not r or len(r) < 2:
            continue
        label = str(r[1] or "").strip()
        head0 = str(r[0] or "").strip()
        if (label.startswith("基金资产净值") or head0 == "资产净值") and out["nav"] is None:
            out["nav"] = _num(r, col_mv)
        if label in ("股票投资", "交易性股票投资") or label.startswith(EQUITY_PREFIX):
            out["stock_mv"] = _num(r, col_mv) or 0.0
            out["stock_pct"] = pct(r)
        if label.startswith("以公允价值计量且其变动计入当期损益的基金投资"):
            out["fund_mv"] = _num(r, col_mv) or 0.0
            out["fund_pct"] = pct(r)
        if label.startswith("存出保证金") and "收益互换协议履约保证金" in label:
            out["swap_pct"] += pct(r) or 0.0
    if not out["nav"] and out["stock_mv"] and out["stock_pct"]:
        out["nav"] = out["stock_mv"] * 100 / out["stock_pct"]
    if not out["nav"] and out["fund_mv"] and out["fund_pct"]:
        out["nav"] = out["fund_mv"] * 100 / out["fund_pct"]
    return out


def find_valuation(root: str, dirname: str, date_str: str) -> Path | None:
    d = Path(root) / dirname
    iso = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    hits = [p for p in glob.glob(str(d / "*"))
            if p.lower().endswith((".xls", ".xlsx"))
            and (date_str in Path(p).name or iso in Path(p).name)]
    if hits:
        return Path(sorted(hits)[0])
    latest = d / "最新估值表.xlsx"
    return latest if latest.is_file() else None


def equity_pct(v: dict) -> float | None:
    """权益敞口 = 股票占净值% + 互换保证金占净值%（互换按股票计）。"""
    if v.get("stock_pct") is None:
        return None
    return round(round(v["stock_pct"], 1) + round(v.get("swap_pct") or 0.0, 1), 1)


# ---------- 渲染 ----------

def wan(x: float | None) -> str:
    return "—" if x is None else f"{x/1e4:,.0f}"


def yi(x: float | None) -> str:
    return "—" if x is None else f"{x/1e8:.2f} 亿"


def iso(d: str | None) -> str:
    return "—" if not d else f"{d[:4]}-{d[4:6]}-{d[6:10]}"


def amount(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x/1e4:,.0f} 万" if x < 1e8 else f"{x/1e8:.2f} 亿"


def fmt_pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.1f}%"


def pct_of(x: float | None, nav: float | None, signed: bool = True) -> str:
    """金额 → 占最新净值%。报告一律用百分比，金额留在底稿 JSON 供核对。"""
    if x is None or not nav:
        return "—"
    v = x / nav * 100
    return f"{v:+.1f}%" if signed else f"{v:.1f}%"


# ---------- 解读用的确定性片段（脚本出数据，AI 只写点评） ----------

def side_cell(items: list[tuple[str, float]], nav: float | None, n: int = 3) -> str:
    """加/减各前 n 名 →「加 A +1.2%；减 B -3.4%、C -1.1%」。"""
    adds = sorted((kv for kv in items if kv[1] > 0), key=lambda kv: -kv[1])[:n]
    cuts = sorted((kv for kv in items if kv[1] < 0), key=lambda kv: kv[1])[:n]
    text_add = "、".join(f"{name} {pct_of(v, nav)}" for name, v in adds) or "—"
    text_cut = "、".join(f"{name} {pct_of(v, nav)}" for name, v in cuts) or "—"
    return f"加 {text_add}；减 {text_cut}"


def reversal_cell(p: dict, n: int = 2) -> str:
    """同一标的近一月与近一周净额符号相反（周内幅度 ≥0.5% 净值）→ 反转。"""
    month_nets = {s["code"]: s["net"] for s in p["month"]["stocks"]}
    rows = []
    for s in p["week"]["stocks"]:
        month_net = month_nets.get(s["code"])
        if not month_net or not s["net"] or month_net * s["net"] > 0:
            continue
        week_pct = s["net"] / p["nav"] * 100
        if abs(week_pct) < 0.5:
            continue
        rows.append((abs(s["net"]), s["name"], month_net / p["nav"] * 100, week_pct))
    rows.sort(reverse=True)
    return "、".join(f"{name} {mp:+.1f}%→{wp:+.1f}%" for _, name, mp, wp in rows[:n]) or "—"


def industry_shift_cell(p: dict, n: int = 2) -> str:
    """近一月 Top 行业 → 近一周：延续 / 放缓 / 反转。"""
    tops = sorted(p["month"]["industries"].items(), key=lambda kv: -abs(kv[1]))[:n]
    out = []
    for name, month_net in tops:
        week_net = p["week"]["industries"].get(name, 0)
        mp, wp = month_net / p["nav"] * 100, week_net / p["nav"] * 100
        if month_net * week_net > 0 and abs(wp) < abs(mp) * 0.5:
            tag = "减仓放缓" if month_net < 0 else "加仓放缓"
        elif month_net * week_net < 0:
            tag = "转为减仓" if week_net < 0 else "转为加仓"
        else:
            tag = "延续"
        out.append(f"{name} {mp:+.1f}%→{wp:+.1f}%（{tag}）")
    return "；".join(out) or "—"


def turnover(w: dict, nav: float | None) -> str:
    return pct_of(w["buy"] + w["sell"], nav, False) if nav else "—"


def net_share(w: dict) -> str:
    gross = w["buy"] + w["sell"]
    return f"{abs(w['net']) / gross * 100:.0f}%" if gross else "—"


def top3_sell_share(w: dict) -> str:
    top = sorted((s for s in w["stocks"] if s["net"] < 0), key=lambda s: s["net"])[:3]
    if not top or not w["sell"]:
        return "—"
    return f"{sum(abs(s['net']) for s in top) / w['sell'] * 100:.0f}%"


def etf_share(w: dict) -> str:
    gross = w["buy"] + w["sell"]
    if not gross:
        return "—"
    etf = sum(s["buy"] + s["sell"] for s in w["stocks"] if s["industry"] == "ETF")
    return f"{etf / gross * 100:.0f}%"


def side_table(items: list[dict], top_n: int, nav: float | None) -> str:
    adds = [i for i in items if i["net"] > 0][:top_n]
    cuts = [i for i in items if i["net"] < 0][-top_n:][::-1]
    lines = ["| 加仓 | 占净值 | 减仓 | 占净值 |", "|---|---|---|---|"]
    for n in range(max(len(adds), len(cuts))):
        a = f"{adds[n]['name']}" if n < len(adds) else ""
        av = pct_of(adds[n]["net"], nav) if n < len(adds) else ""
        c = f"{cuts[n]['name']}" if n < len(cuts) else ""
        cv = pct_of(cuts[n]["net"], nav) if n < len(cuts) else ""
        lines.append(f"| {a} | {av} | {c} | {cv} |")
    return "\n".join(lines)


def industry_table(industries: dict[str, int], top_n: int, nav: float | None) -> str:
    items = sorted(industries.items(), key=lambda x: -x[1])
    adds = [(k, v) for k, v in items if v > 0][:top_n]
    cuts = [(k, v) for k, v in items if v < 0][-top_n:][::-1]
    lines = ["| 加仓行业 | 占净值 | 减仓行业 | 占净值 |", "|---|---|---|---|"]
    for n in range(max(len(adds), len(cuts))):
        a = adds[n][0] if n < len(adds) else ""
        av = pct_of(adds[n][1], nav) if n < len(adds) else ""
        c = cuts[n][0] if n < len(cuts) else ""
        cv = pct_of(cuts[n][1], nav) if n < len(cuts) else ""
        lines.append(f"| {a} | {av} | {c} | {cv} |")
    return "\n".join(lines)


def render_md(report: dict, cfg: dict) -> str:
    w, m = report["windows"]["week"], report["windows"]["month"]
    top_n = cfg["top_n"]
    L = [f"# 近期操作分析 {report['date_iso']}", ""]
    L.append(f"> 成交：`{Path(cfg['trades_csv']).name}`（截至 {iso(report['trade_end'])}）")
    L.append(f"> 仓位：`{cfg['valuation_root']}` 下各产品估值表（截至 {iso(report['valuation_end'])}）")
    L.append(f"> 窗口：近一周 = 最近 {cfg['windows']['week']} 个交易日（{iso(w[0])} ~ {iso(w[1])}）；"
             f"近一月 = 最近 {cfg['windows']['month']} 个交易日（{iso(m[0])} ~ {iso(m[1])}）")
    products = report["products"]
    L += ["", "## 解读", ""]

    # 1 个股动向：脚本给加/减 Top3 与单票反转，AI 不给结论
    L += ["### 1. 个股动向（占本产品最新净值%）", "",
          "| 产品 | 近一周 | 近一月 | 单票反转（近一月→近一周） |", "|---|---|---|---|"]
    for p in products:
        L.append(f"| {p['name']} "
                 f"| {side_cell([(s['name'], s['net']) for s in p['week']['stocks']], p['nav'])} "
                 f"| {side_cell([(s['name'], s['net']) for s in p['month']['stocks']], p['nav'])} "
                 f"| {reversal_cell(p)} |")
    L.append("")

    # 2 行业动向：脚本给行业加/减 Top3 与主线变化标注
    L += ["### 2. 行业动向（占本产品最新净值%）", "",
          "| 产品 | 近一周 | 近一月 | 主线变化（近一月→近一周） |", "|---|---|---|---|"]
    for p in products:
        L.append(f"| {p['name']} "
                 f"| {side_cell(list(p['week']['industries'].items()), p['nav'])} "
                 f"| {side_cell(list(p['month']['industries'].items()), p['nav'])} "
                 f"| {industry_shift_cell(p)} |")
    L.append("")

    # 3 交易特征：换手、净卖出占比、卖压集中度、ETF 占比
    L += ["### 3. 交易特征", "",
          "| 产品 | 近一周换手 | 近一周净÷成交 | 近一周前三卖单占卖出 | 近一月换手 | 近一月前三卖单占卖出 | 近一月ETF占成交 |",
          "|---|---|---|---|---|---|---|"]
    for p in products:
        wk, mo = p["week"], p["month"]
        L.append(f"| {p['name']} | {turnover(wk, p['nav'])} | {net_share(wk)} | {top3_sell_share(wk)} "
                 f"| {turnover(mo, p['nav'])} | {top3_sell_share(mo)} | {etf_share(mo)} |")
    L.append("")

    # 4 产品分化：按各产品自身净额排序，不做跨产品加总
    L += ["### 4. 产品分化", "",
          "| 产品 | 净值（≈） | 近一周净 | 近一月净 | 期末权益敞口 |", "|---|---|---|---|---|"]
    for p in sorted(products, key=lambda x: (x["week"]["net"] / x["nav"]) if x["nav"] else 0):
        L.append(f"| {p['name']} | {yi(p['nav'])} | {pct_of(p['week']['net'], p['nav'])} "
                 f"| {pct_of(p['month']['net'], p['nav'])} | {fmt_pct(p['position'].get('equity'))} |")
    L.append("")

    # 5 各产品点评：唯一由 AI 写的部分
    L += ["### 5. 各产品点评", "",
          "<!-- AI：逐产品 0–3 句，只写判断（为啥这么动、值不值得跟踪），不复述上表数字 -->", ""]
    for p in products:
        L.append(f"- **{p['name']}**：<!-- AI 填写 -->")
    L.append("")

    L += ["## 口径", ""]
    L.append("- 加减按「占本产品最新净值%」，金额见《操作底稿》JSON；净买卖 ≈ 仓位变化（未扣市场损益）。")
    L.append("- 成交截至报告日、仓位截至上一估值日，两者错位时净卖出与敞口变化不完全同步属正常。")
    special = []
    for p in products:
        if p.get("note") and p.get("own_nav"):
            special.append(f"{p['name']} 按母基金口径（自身净值约 {amount(p['own_nav'])}）")
        if (p["position"].get("swap") or 0) > 0:
            special.append(f"{p['name']} 权益敞口含互换保证金 {p['position']['swap']:.1f} 个百分点")
    if special:
        L.append("- " + "；".join(special) + "。完整口径见 SKILL.md。")
    else:
        L.append("- 完整口径见 SKILL.md。")
    L += ["", "## 总览（占最新净值%）", "",
          "| 产品 | 净值（≈） | 近一周 买 / 卖 | 近一周 净 | 近一月 买 / 卖 | 近一月 净 |",
          "|---|---|---|---|---|---|"]
    for p in report["products"]:
        wk, mo = p["week"], p["month"]
        L.append(f"| {p['name']} | {yi(p['nav'])} "
                 f"| {pct_of(wk['buy'], p['nav'], False)} / {pct_of(wk['sell'], p['nav'], False)} "
                 f"| **{pct_of(wk['net'], p['nav'])}** "
                 f"| {pct_of(mo['buy'], p['nav'], False)} / {pct_of(mo['sell'], p['nav'], False)} "
                 f"| {pct_of(mo['net'], p['nav'])} |")
    # 不做「全部产品合计」：各产品净值规模与策略不同，跨产品百分比加总没有组合含义。
    repo_notes = [
        f"{p['name']} 近一周 {pct_of(p['week']['repo'], p['nav'])}、近一月 {pct_of(p['month']['repo'], p['nav'])}"
        for p in report["products"]
        if p["week"]["repo"] or p["month"]["repo"]
    ]
    if repo_notes:
        L.append("\n> 另有回购操作（不计入个股）：" + "；".join(repo_notes) + "。")
    L += ["", "## 仓位（最新估值表）", "",
          "| 产品 | 估值日 | 净值（≈） | 权益敞口（股票+互换） | 其中直接股票 | 其中互换保证金 | 近一月净买卖（≈变化） |",
          "|---|---|---|---|---|---|---|"]
    for p in report["products"]:
        pos = p["position"]
        L.append(f"| {p['name']} | {iso(pos['date'])} | {yi(p['nav'])} | **{fmt_pct(pos['equity'])}** "
                 f"| {fmt_pct(pos['stock'])} | {fmt_pct(pos['swap'])} "
                 f"| {pct_of(p['month']['net'], p['nav'])} |")
    for label, key in (("近一周", "week"), ("近一月", "month")):
        L += ["", f"## {label}（分产品）"]
        for p in report["products"]:
            L += ["", f"<details><summary>{p['name']}：{label}净 {pct_of(p[key]['net'], p['nav'])}</summary>", "",
                  "**行业（占本产品最新净值%）**", "",
                  industry_table(p[key]["industries"], top_n, p["nav"]), "",
                  "**个股（占本产品最新净值%）**", "",
                  side_table(p[key]["stocks"], top_n, p["nav"]), "", "</details>"]
    return "\n".join(L) + "\n"


def print_report(report: dict) -> None:
    for p in report["products"]:
        pos = p["position"]
        print(f"{p['name']}: 净值≈{yi(p['nav'])} 权益敞口 {fmt_pct(pos['equity'])}"
              f"（股票 {fmt_pct(pos['stock'])} + 互换 {fmt_pct(pos['swap'])}）")
        for key, label in (("week", "近一周"), ("month", "近一月")):
            w = p[key]
            print(f"  {label} {w['start']}~{w['end']}: 买 {pct_of(w['buy'], p['nav'], False)}"
                  f" / 卖 {pct_of(w['sell'], p['nav'], False)} / 净 {pct_of(w['net'], p['nav'])}")
    unresolved = sorted({c for p in report["products"] for k in ("week", "month") for c in p[k]["unresolved"]})
    if unresolved:
        print(f"[待补行业] {', '.join(unresolved)}")


# ---------- 主流程 ----------

def build_report(cfg: dict, end_date: str | None = None) -> dict:
    overrides = cfg["industry_overrides"]
    trades = load_trades(cfg["trades_csv"], {n for p in cfg["products"] for n in p["csv_names"]})
    end = end_date or max(t["date"] for t in trades)
    windows = build_windows([t["date"] for t in trades], end, cfg["windows"])
    val_end = max(
        (t["date"] for t in trades if t["date"] < end),
        default=end,
    )
    products, tot = [], {k: {"buy": 0, "sell": 0, "net": 0, "repo": 0, "nav": 0.0,
                             "industries": defaultdict(float), "stocks": [], "start": "", "end": ""}
                         for k in windows}
    for p in cfg["products"]:
        names = set(p["csv_names"])
        entry = {"name": p["name"], "note": p.get("note", ""), "nav": None, "position": {}}
        own = parse_valuation(fp) if (fp := find_valuation(cfg["valuation_root"], p["valuation_dir"], val_end)) else {}
        pos_v = own if p["position_dir"] == p["valuation_dir"] else (
            parse_valuation(fp2) if (fp2 := find_valuation(cfg["valuation_root"], p["position_dir"], val_end)) else {})
        entry["nav"] = pos_v.get("nav") or own.get("nav")
        entry["own_nav"] = None
        if p.get("own_valuation_dir") and p["own_valuation_dir"] != p["valuation_dir"]:
            ov = find_valuation(cfg["valuation_root"], p["own_valuation_dir"], val_end)
            entry["own_nav"] = parse_valuation(ov).get("nav") if ov else None
        # 仓位只取最新估值表：资产规模与权益敞口都以最新一日为准（用户口径：取个大概即可）
        entry["position"] = {
            "date": pos_v.get("date") or val_end,
            "equity": equity_pct(pos_v),
            "stock": pos_v.get("stock_pct"),
            "swap": pos_v.get("swap_pct"),
        }
        for key, (a, b) in windows.items():
            agg = aggregate(trades, names, a, b, overrides)
            entry[key] = agg
            t = tot[key]
            t["buy"] += agg["buy"]; t["sell"] += agg["sell"]; t["net"] += agg["net"]; t["repo"] += agg["repo"]
            for k, v in agg["industries"].items():
                t["industries"][k] += v
        products.append(entry)
    for key in windows:
        tot[key]["nav"] = sum(p["nav"] or 0 for p in products) or None
        codes = defaultdict(lambda: [0.0, 0.0, "", ""])
        for p in products:
            for it in p[key]["stocks"]:
                row = codes[it["code"]]
                row[0] += it["buy"]; row[1] += it["sell"]; row[2] = it["name"]; row[3] = it["industry"]
        tot[key]["stocks"] = sorted(
            ({"code": c, "name": v[2], "industry": v[3], "buy": round(v[0]), "sell": round(v[1]),
              "net": round(v[0] - v[1])} for c, v in codes.items()),
            key=lambda x: -x["net"])
        tot[key]["industries"] = {k: round(v) for k, v in sorted(tot[key]["industries"].items(), key=lambda x: -x[1])}
        tot[key]["start"], tot[key]["end"] = windows[key]
        tot[key]["unresolved"] = sorted({i["code"] for i in tot[key]["stocks"] if i["industry"] == UNKNOWN})
    return {
        "date_iso": f"{end[:4]}-{end[4:6]}-{end[6:]}",
        "trade_end": end,
        "valuation_end": products[0]["position"]["date"] if products else "",
        "windows": windows,
        "products": products,
        "total": tot,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="近期操作分析：成交 + 估值表 → 行业/个股加减与仓位变化")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--date", help="报告日 YYYYMMDD，默认取成交数据最后一天")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    cfg = load_config(args.config)
    report = build_report(cfg, args.date)
    out_dir = Path(cfg["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"操作分析_{report['date_iso']}"
    (out_dir / f"{stem}.md").write_text(render_md(report, cfg), encoding="utf-8")
    (out_dir / f"操作底稿_{report['date_iso']}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=list), encoding="utf-8")
    print_report(report)
    print(f"\n[ok] {out_dir / (stem + '.md')}")
    print(f"[ok] {out_dir / ('操作底稿_' + report['date_iso'] + '.json')}")
    return 0


# ---------- 自检（离线，不读外部文件） ----------

def selftest() -> int:
    overrides = {"204001.SH": ["GC001", "回购"], "510300.SH": ["沪深300ETF", "ETF"],
                 "603806.SH": ["福斯特", "电力设备-光伏胶膜"]}
    info = {"603259.SH": ["药明康德", "医药生物-CXO"],
            "002475.SZ": ["立讯精密", "消费电子-精密制造"]}
    # 9 个交易日；窗口 = 最近 5 / 最近 20（不足则取全部）
    cal = ["20261102", "20261103", "20261104", "20261105", "20261106",
           "20261107", "20261110", "20261111", "20261112"]
    trades = [
        {"date": cal[-1], "prod": "A", "code": "603259.SH", "name": "药明康德", "side": "卖出", "amt": 1000},
        {"date": cal[-1], "prod": "A", "code": "002475.SZ", "name": "立讯精密", "side": "卖出", "amt": 500},
        {"date": cal[-1], "prod": "B", "code": "603806.SH", "name": "福斯特", "side": "买入", "amt": 300},
        {"date": cal[-2], "prod": "A", "code": "603259.SH", "name": "药明康德", "side": "买入", "amt": 400},
        {"date": cal[0], "prod": "A", "code": "603259.SH", "name": "药明康德", "side": "买入", "amt": 7000},
        {"date": cal[0], "prod": "A", "code": "204001.SH", "name": "GC001", "side": "卖出", "amt": 888},
        {"date": cal[-2], "prod": "A", "code": "999999.SH", "name": "HAJ123", "side": "买入", "amt": 50},
    ]
    wins = build_windows(cal, cal[-1], {"week": 5, "month": 20})
    assert wins["week"] == (cal[-5], cal[-1]), wins
    assert wins["month"] == (cal[0], cal[-1]), wins  # 不足 20 天取全部

    agg = aggregate(trades, {"A"}, *wins["week"], {**overrides, **info})
    assert agg["buy"] == 450 and agg["sell"] == 1500, agg   # 400 买 + 50 未归类买 / 1000+500 卖
    assert agg["repo"] == 0, agg                            # 回购在窗口外
    assert agg["industries"]["医药生物"] == -600, agg        # 1000-400
    assert agg["industries"]["消费电子"] == -500, agg
    assert agg["industries"][UNKNOWN] == 50, agg
    assert agg["unresolved"] == ["999999.SH"], agg

    month = aggregate(trades, {"A"}, *wins["month"], {**overrides, **info})
    assert month["repo"] == 888, month                      # 回购单独计，不进买卖
    assert month["industries"]["医药生物"] == -600 + 7000, month

    # 权益敞口合并：股票 + 互换保证金
    assert equity_pct({"stock_pct": 48.4, "swap_pct": 21.2}) == 69.6
    assert equity_pct({"stock_pct": 92.1, "swap_pct": 0.0}) == 92.1
    assert equity_pct({"stock_pct": None}) is None
    # 无互换时不应把 None 当 0 加出 NaN
    assert equity_pct({"stock_pct": 66.0}) == 66.0

    # 估值表解析：构造最小行集（含互换保证金与净值行）
    rows = [
        ["证券投资基金估值表"],
        ["估值日期：20261103"],
        ["科目代码", "科目名称", "数量", "单位成本", "成本", "成本占净值%", "市价", "市值", "市值占净值%"],
        ["1002", "银行存款", "", "", "1", "0.01", "", "1", "0.01"],
        ["110101", "股票投资", "", "", "600", "60", "", "700", "70"],
        ["103132", "存出保证金_收益互换协议履约保证金", "", "", "100", "10", "", "100", "10"],
        ["基金资产净值:VP600B", "", "", "", "", "", "", "1000", "100"],
    ]
    v = parse_valuation_rows(rows)
    assert v["date"] == "20261103", v
    assert v["stock_pct"] == 70 and v["swap_pct"] == 10, v
    assert v["nav"] == 1000, v
    assert equity_pct(v) == 80.0, v

    # 另一种模板：三行重复表头、市值分原币/本币、占比为 0–1 小数、股票科目标「交易性股票投资」
    rows2 = [
        ["SAUY87_某产品_资产估值表"],
        ["日期：2026-11-03", "单位净值:1.2590"],
        ["科目代码", "科目名称", "币种", "汇率", "数量", "单位成本", "成本", "成本", "成本占比"],
        ["科目代码", "科目名称", "币种", "汇率", "数量", "单位成本", "原币", "本币", "成本占比",
         "行情", "市值", "市值", "市值占比"],
        ["1102", "交易性股票投资", "***", "", "", "", "", "79464401.61", "1.21922", "0", "", "65055511.11", "0.998144"],
        ["资产净值", " ", " ", "", "", "", "", "79585337.35", "1.221075", "0", "", "65176446.85", "1"],
    ]
    v2 = parse_valuation_rows(rows2)
    assert v2["date"] == "20261103", v2
    assert abs(v2["stock_pct"] - 99.8) < 0.05, v2     # 0.998144 × 100
    assert abs(v2["nav"] - 65176446.85) < 1, v2
    print("selftest PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
