#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""近期操作分析：成交 CSV + 估值表 → 近一周/近一月 的行业与个股加减、仓位变化。

只做确定性抽取与汇总；「为什么动、意味着什么」留给 AI（见 SKILL.md 阶段二）。

口径（与 SKILL.md 一致，改口径先改这里）：
- 窗口按**交易日**数：近一周 = 最近 5 个交易日，近一月 = 最近 20 个交易日。
  交易日取自成交 CSV 自身的日期集合（该数据源每日落盘，无需另建日历）。
- 权益敞口 = 股票市值占净值% + 收益互换履约保证金占净值%。
  互换底层股票不在估值表内，保证金按 1:1 视为敞口，仅臻享1期存在。
- 回购（GC001 等）不计入股票加减，单独统计。
- 行业取标的信息库 industry 的一级（「半导体-晶圆代工」→「半导体」），
  信息库缺失的标的由 config 的 industry_overrides 补齐；仍未归类的写进输出待 AI 处理。

产物（写 config.output_dir）：
- 操作分析_YYYY-MM-DD.md   表格 + 「解读」占位（AI 填写）
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
    cfg.setdefault("info_db", "")
    for p in cfg["products"]:
        p.setdefault("valuation_dir", p["position_dir"])
    return cfg


def load_info_db(path: str) -> dict:
    if not path:
        return {}
    fp = Path(path)
    if not fp.is_file():
        print(f"[warn] 标的信息库不存在，行业全部回退 overrides: {fp}", file=sys.stderr)
        return {}
    return {x["code"]: x for x in json.loads(fp.read_text(encoding="utf-8"))["stocks"]}


def load_trades(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
        for r in csv.DictReader(f):
            if r.get("委托方向") not in (BUY, SELL):
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


def resolve(code: str, info: dict, overrides: dict) -> tuple[str, str]:
    """返回 (名称, 行业一级)。"""
    e = info.get(code)
    if e and e.get("industry"):
        return e.get("name") or code, e["industry"].split("-")[0]
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
              info: dict, overrides: dict) -> dict:
    picked = [t for t in trades if t["prod"] in csv_names and start <= t["date"] <= end]
    stocks: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    repo_amt = 0.0
    buy = sell = 0.0
    for t in picked:
        _, industry = resolve(t["code"], info, overrides)
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
        name, industry = resolve(code, info, overrides)
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
    hdr_i = next((i for i, r in enumerate(rows) if r and str(r[0]).strip() == "科目代码"), None)
    if hdr_i is None:
        raise SystemExit(f"估值表结构不认识（找不到表头）: {source}")
    hdr = [str(c).strip() if c is not None else "" for c in rows[hdr_i]]
    col_mv = hdr.index("市值")
    col_mvpct = hdr.index("市值占净值%") if "市值占净值%" in hdr else None

    out = {"nav": None, "stock_pct": None, "stock_mv": 0.0,
           "fund_pct": None, "fund_mv": 0.0, "swap_pct": 0.0, "date": ""}
    # 估值日期在表头上方（第 2 行），不在明细区
    for r in rows[:hdr_i]:
        if r and "估值日期" in str(r[0] or ""):
            out["date"] = str(r[0]).split("：", 1)[-1].strip()[:10].replace("-", "")
    for r in rows[hdr_i + 1:]:
        if not r or len(r) < 2:
            continue
        label = str(r[1] or "").strip()
        if label.startswith("基金资产净值") and out["nav"] is None:
            out["nav"] = _num(r, col_mv)
        if label == "股票投资" or label.startswith(EQUITY_PREFIX):
            out["stock_mv"] = _num(r, col_mv) or 0.0
            out["stock_pct"] = _num(r, col_mvpct) if col_mvpct is not None else None
        if label.startswith("以公允价值计量且其变动计入当期损益的基金投资"):
            out["fund_mv"] = _num(r, col_mv) or 0.0
            out["fund_pct"] = _num(r, col_mvpct) if col_mvpct is not None else None
        if label.startswith("存出保证金") and "收益互换协议履约保证金" in label:
            out["swap_pct"] += _num(r, col_mvpct) or 0.0
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
    """权益敞口 = 股票占净值% + 互换保证金占净值%（保证金按 1:1 视为敞口）。"""
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


def side_table(items: list[dict], top_n: int) -> str:
    adds = [i for i in items if i["net"] > 0][:top_n]
    cuts = [i for i in items if i["net"] < 0][-top_n:][::-1]
    lines = ["| 加仓 | 净额（万） | 减仓 | 净额（万） |", "|---|---|---|---|"]
    for n in range(max(len(adds), len(cuts))):
        a = f"{adds[n]['name']}" if n < len(adds) else ""
        av = wan(adds[n]["net"]) if n < len(adds) else ""
        c = f"{cuts[n]['name']}" if n < len(cuts) else ""
        cv = wan(cuts[n]["net"]) if n < len(cuts) else ""
        lines.append(f"| {a} | {av} | {c} | {cv} |")
    return "\n".join(lines)


def industry_table(industries: dict[str, int], top_n: int) -> str:
    items = sorted(industries.items(), key=lambda x: -x[1])
    adds = [(k, v) for k, v in items if v > 0][:top_n]
    cuts = [(k, v) for k, v in items if v < 0][-top_n:][::-1]
    lines = ["| 加仓行业 | 净额（万） | 减仓行业 | 净额（万） |", "|---|---|---|---|"]
    for n in range(max(len(adds), len(cuts))):
        a = adds[n][0] if n < len(adds) else ""
        av = wan(adds[n][1]) if n < len(adds) else ""
        c = cuts[n][0] if n < len(cuts) else ""
        cv = wan(cuts[n][1]) if n < len(cuts) else ""
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
    L += ["", "## 口径", ""]
    L.append("- **权益敞口 = 股票市值占净值% + 收益互换履约保证金占净值%**。互换底层股票不在估值表内，保证金按 1:1 视为敞口（仅臻享1期存在互换，实际杠杆未知）。")
    L.append("- 回购（GC001 等）不计入个股加减，单独列出。")
    L.append("- 成交截至报告日、估值截至上一估值日，净卖出额与仓位变化不完全对齐属正常。")
    L.append("- 行业为标的信息库一级分类；未覆盖标的按 config 的 industry_overrides 补齐。")
    for p in report["products"]:
        if p.get("note"):
            own = f"（自身净值约 {amount(p['own_nav'])}；个股操作与仓位按母基金口径）" if p.get("own_nav") else ""
            L.append(f"- **{p['name']}**：{p['note']}{own}")
    L += ["", "## 总览", "", "| 产品 | 净值（≈） | 近一周 买 / 卖 | 近一周 净 | 近一月 买 / 卖 | 近一月 净 |",
          "|---|---|---|---|---|---|"]
    for p in report["products"]:
        L.append(f"| {p['name']} | {yi(p['nav'])} | {wan(p['week']['buy'])} / {wan(p['week']['sell'])} 万 "
                 f"| **{wan(p['week']['net'])} 万** | {wan(p['month']['buy'])} / {wan(p['month']['sell'])} 万 "
                 f"| {wan(p['month']['net'])} 万 |")
    tb = report["total"]
    L.append(f"| **合计** | — | {wan(tb['week']['buy'])} / {wan(tb['week']['sell'])} 万 | "
             f"**{wan(tb['week']['net'])} 万** | {wan(tb['month']['buy'])} / {wan(tb['month']['sell'])} 万 "
             f"| {wan(tb['month']['net'])} 万 |")
    if tb["week"]["repo"] or tb["month"]["repo"]:
        L.append(f"\n> 另有回购操作：近一周 {wan(tb['week']['repo'])} 万、近一月 {wan(tb['month']['repo'])} 万（不计入个股）。")
    L += ["", "## 仓位", "", "| 产品 | 期初 | 期末 | 权益敞口 期初 → 期末 | 变化 | 其中股票 | 其中互换保证金 |",
          "|---|---|---|---|---|---|---|"]
    for p in report["products"]:
        pos = p["position"]
        delta = "—" if pos["delta"] is None else f"{pos['delta']:+.1f}pp"
        L.append(f"| {p['name']} | {iso(pos['start_date'])} | {iso(pos['end_date'])} "
                 f"| {fmt_pct(pos['equity_start'])} → {fmt_pct(pos['equity_end'])} "
                 f"| {delta} | {fmt_pct(pos['stock_end'])} | {fmt_pct(pos['swap_end'])} |")
    for label, key in (("近一周", "week"), ("近一月", "month")):
        L += ["", f"## {label}", "", "### 行业", "", industry_table(tb[key]["industries"], top_n),
              "", "### 个股（三产品合计）", "", side_table(tb[key]["stocks"], top_n)]
        for p in report["products"]:
            L += ["", f"<details><summary>{p['name']}：合计净 {wan(p[key]['net'])} 万</summary>", "",
                  industry_table(p[key]["industries"], top_n), "", side_table(p[key]["stocks"], top_n), "", "</details>"]
    L += ["", "## 解读", "", "<!-- AI：读《操作底稿》后填写，3–5 条，带数字，不评价「对不对」 -->", ""]
    return "\n".join(L)


def print_report(report: dict) -> None:
    for p in report["products"]:
        pos = p["position"]
        print(f"{p['name']}: 净值≈{yi(p['nav'])} 权益敞口 "
              f"{fmt_pct(pos['equity_start'])}→{fmt_pct(pos['equity_end'])} "
              f"({pos['delta'] if pos['delta'] is None else f'{pos['delta']:+.1f}pp'})")
        for key, label in (("week", "近一周"), ("month", "近一月")):
            w = p[key]
            print(f"  {label} {w['start']}~{w['end']}: 买 {wan(w['buy'])}万 / 卖 {wan(w['sell'])}万 / 净 {wan(w['net'])}万")
    unresolved = sorted({c for p in report["products"] for k in ("week", "month") for c in p[k]["unresolved"]})
    if unresolved:
        print(f"[待补行业] {', '.join(unresolved)}")


# ---------- 主流程 ----------

def build_report(cfg: dict, end_date: str | None = None) -> dict:
    trades = load_trades(cfg["trades_csv"])
    info = load_info_db(cfg.get("info_db", ""))
    overrides = cfg["industry_overrides"]
    end = end_date or max(t["date"] for t in trades)
    windows = build_windows([t["date"] for t in trades], end, cfg["windows"])
    val_end = max(
        (t["date"] for t in trades if t["date"] < end),
        default=end,
    )
    products, tot = [], {k: {"buy": 0, "sell": 0, "net": 0, "repo": 0,
                             "industries": defaultdict(float), "stocks": [], "start": "", "end": ""}
                         for k in windows}
    for p in cfg["products"]:
        names = set(p["csv_names"])
        entry = {"name": p["name"], "note": p.get("note", ""), "nav": None, "position": {}}
        own = parse_valuation(fp) if (fp := find_valuation(cfg["valuation_root"], p["valuation_dir"], val_end)) else {}
        pos_v_end = own if p["position_dir"] == p["valuation_dir"] else (
            parse_valuation(fp2) if (fp2 := find_valuation(cfg["valuation_root"], p["position_dir"], val_end)) else {})
        entry["nav"] = own.get("nav")
        entry["own_nav"] = None
        if p.get("own_valuation_dir") and p["own_valuation_dir"] != p["valuation_dir"]:
            ov = find_valuation(cfg["valuation_root"], p["own_valuation_dir"], val_end)
            entry["own_nav"] = parse_valuation(ov).get("nav") if ov else None
        # 期初：与近一月窗口起点对齐
        start = windows["month"][0]
        pos_v_start = (parse_valuation(fp3) if (fp3 := find_valuation(cfg["valuation_root"], p["position_dir"], start)) else {})
        e0, e1 = equity_pct(pos_v_start), equity_pct(pos_v_end)
        entry["position"] = {
            "start_date": pos_v_start.get("date") or start,
            "end_date": pos_v_end.get("date") or val_end,
            "equity_start": e0, "equity_end": e1,
            "delta": None if (e0 is None or e1 is None) else round(e1 - e0, 1),
            "stock_start": pos_v_start.get("stock_pct"), "stock_end": pos_v_end.get("stock_pct"),
            "swap_start": pos_v_start.get("swap_pct"), "swap_end": pos_v_end.get("swap_pct"),
        }
        for key, (a, b) in windows.items():
            agg = aggregate(trades, names, a, b, info, overrides)
            entry[key] = agg
            t = tot[key]
            t["buy"] += agg["buy"]; t["sell"] += agg["sell"]; t["net"] += agg["net"]; t["repo"] += agg["repo"]
            for k, v in agg["industries"].items():
                t["industries"][k] += v
        products.append(entry)
    for key in windows:
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
        "valuation_end": products[0]["position"]["end_date"] if products else "",
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
    info = {"603259.SH": {"name": "药明康德", "industry": "医药生物-CXO"},
            "002475.SZ": {"name": "立讯精密", "industry": "消费电子-精密制造"}}
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

    agg = aggregate(trades, {"A"}, *wins["week"], info, overrides)
    assert agg["buy"] == 450 and agg["sell"] == 1500, agg   # 400 买 + 50 未归类买 / 1000+500 卖
    assert agg["repo"] == 0, agg                            # 回购在窗口外
    assert agg["industries"]["医药生物"] == -600, agg        # 1000-400
    assert agg["industries"]["消费电子"] == -500, agg
    assert agg["industries"][UNKNOWN] == 50, agg
    assert agg["unresolved"] == ["999999.SH"], agg

    month = aggregate(trades, {"A"}, *wins["month"], info, overrides)
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
    print("selftest PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
