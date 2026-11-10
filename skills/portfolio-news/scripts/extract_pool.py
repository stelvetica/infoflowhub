"""从持仓 Excel 抽取标的池，输出 JSON。

职责（确定性，AI 不参与）：
- 读 Excel 固定列（col 0 产品名 / col 1 代码 / col 2 名称 / col 6 仓位）。
- 去重得到股票列表，按最高单产品仓位降序。
- 仅保留至少一个产品仓位 > 0 的股票。
- 输出 JSON：每只股票含 code/name/holders/max_weight；A 股若存在同名港股，
  另挂 hk_code 字段（港股孪生计，不单独计仓、不重复计入 count）。
  A+H 公司 A 股公告常发「自愿性公告」这类壳标题，港股通道标题/类型更具体，
  该字段供 collect_announcements 借来判类与补收港股独有实质公告。

不承担：行业/产业链/主营业务归类（由阶段二 AI 维护标的信息库）。

路径：默认读 C:/Users/ETT/Playground/infoflowhub/data/标的观察/持仓标的.xlsx，输出到主 checkout 的
infoflowhub/data/reports/标的池.json（产物根）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from openpyxl import load_workbook

DEFAULT_EXCEL_PATH = Path(r"C:\Users\ETT\Playground\infoflowhub\data\标的观察\持仓标的.xlsx")
DEFAULT_OUTPUT_PATH = Path(r"C:\Users\ETT\Playground\infoflowhub\data\reports\标的池.json")
SUGGEST_API = ("https://searchapi.eastmoney.com/api/suggest/get?input={q}"
               "&type=14&token=D43BF722C8E33BDC906FB84D85E326E8&count=10")


def normalize_hk_code(code: str) -> str:
    """02359 → 02359.HK（五位补零，与 collect_announcements.to_em_code 对齐）"""
    return "%05d.HK" % int(code)


def match_hk_twin(rows: list[dict], name: str) -> str | None:
    """东财 suggest 结果 → 同名港股代码；无同名港股返回 None（期货等同名条目被类型过滤）"""
    for r in rows:
        if (r.get("SecurityTypeName") == "港股"
                and str(r.get("Name", "")).strip() == name.strip()):
            return normalize_hk_code(str(r.get("Code")))
    return None


def fetch_suggest(name: str, retries: int = 2) -> list[dict]:
    url = SUGGEST_API.format(q=urllib.parse.quote(name))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.load(r)
            return (data.get("QuotationCodeTable") or {}).get("Data") or []
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(1)
    return []


def hk_held_codes(rows: list[dict]) -> set[int]:
    """池内已直接持有的港股（数值比较，容忍 0981.HK / 981.HK 写法差异）"""
    return {int(r["code"].split(".")[0]) for r in rows if r["code"].endswith(".HK")}


def attach_hk_twins(rows: list[dict], resolver: Callable[[str], str | None]) -> None:
    """给 A 股挂 hk_code；港股已在池中（同公司 A+H 双录）则不挂，避免重复取公告。
    解析失败的标的只告警不中断——单只拿不到别名不应拖垮整个标的池。"""
    held = hk_held_codes(rows)
    for r in rows:
        if not r["code"].endswith((".SH", ".SZ")):
            continue
        try:
            twin = resolver(r["name"])
        except Exception as e:
            print(f"警告：{r['code']} {r['name']} 港股孪生解析失败（{type(e).__name__}），跳过",
                  file=sys.stderr)
            continue
        if twin and int(twin.split(".")[0]) not in held:
            r["hk_code"] = twin


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="从持仓 Excel 抽取标的池 JSON")
    p.add_argument(
        "excel_path", nargs="?", type=Path, default=DEFAULT_EXCEL_PATH,
        help="Excel 文件路径；不传则默认读取固定持仓文件",
    )
    p.add_argument(
        "-o", "--output", type=Path, default=DEFAULT_OUTPUT_PATH,
        help="输出 JSON 路径",
    )
    p.add_argument("--selftest", action="store_true", help="只跑内置断言，不读 Excel、不联网")
    return p.parse_args()


def detect_sheet_name(workbook: Any) -> str:
    sheetnames: list[object] = getattr(workbook, "sheetnames", [])
    for sheet in sheetnames:
        if "持仓" in str(sheet):
            return str(sheet)
    raise ValueError(f"未找到包含“持仓”的工作表，可用工作表：{sheetnames}")


def to_float(value: object) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return float(value)
    raise TypeError(f"不支持的数值类型：{type(value)!r}")


def load_pool(excel_path: Path) -> tuple[datetime | None, list[str], dict[str, dict]]:
    """读 Excel，返回 (数据日期, 产品顺序, {代码: {name, weights: {产品: 仓位}}})。"""
    workbook = load_workbook(excel_path, data_only=True)
    sheet_name = detect_sheet_name(workbook)
    ws = workbook[sheet_name]

    as_of_raw = ws.cell(1, 1).value
    as_of = as_of_raw if isinstance(as_of_raw, datetime) else None

    stocks: dict[str, dict] = {}
    product_order: list[str] = []

    for row in ws.iter_rows(min_row=2, values_only=True):
        product_raw, code_raw, name_raw = row[0], row[1], row[2]
        if not product_raw or not code_raw or not name_raw:
            continue

        product = str(product_raw).strip()
        code = str(code_raw).strip()
        name = str(name_raw).strip()
        weight = to_float(row[6] if len(row) > 6 else 0.0)

        if product not in product_order:
            product_order.append(product)

        if code not in stocks:
            stocks[code] = {"name": name, "weights": {}}
        stocks[code]["weights"][product] = weight

    # 补齐缺失产品为 0
    for payload in stocks.values():
        for product in product_order:
            payload["weights"].setdefault(product, 0.0)

    return as_of, product_order, stocks


def build_pool(as_of: datetime | None, product_order: list[str],
               stocks: dict[str, dict]) -> dict:
    """过滤空仓 + 排序，构造输出结构。"""
    rows: list[dict] = []
    for code, payload in stocks.items():
        weights = [payload["weights"].get(p, 0.0) for p in product_order]
        if max(weights) <= 0:
            continue
        rows.append({
            "code": code,
            "name": payload["name"],
            "holders": [p for p, w in zip(product_order, weights) if w > 0],
            "max_weight": round(max(weights), 4),
        })

    rows.sort(key=lambda r: (-r["max_weight"], r["code"]))

    return {
        "as_of": as_of.strftime("%Y-%m-%d") if as_of else None,
        "product_order": product_order,
        "count": len(rows),
        "stocks": rows,
    }


def main() -> None:
    args = parse_args()
    if args.selftest:
        rows = [{"Code": "603259", "Name": "药明康德", "SecurityTypeName": "沪A"},
                {"Code": "02359", "Name": "药明康德", "SecurityTypeName": "港股"},
                {"Code": "SMCM7", "Name": "药明康德期货", "SecurityTypeName": "期货"}]
        assert match_hk_twin(rows, "药明康德") == "02359.HK"
        assert match_hk_twin(rows, "中芯国际") is None
        assert normalize_hk_code("981") == "00981.HK"
        # 别名：A 股挂孪生；港股已在池中（华勤 A+H 双录）不挂；无港股同名不挂
        fake = {"华勤技术": "03296.HK", "药明康德": "02359.HK", "中芯国际": None}
        sample = [{"code": "603296.SH", "name": "华勤技术"},
                  {"code": "3296.HK", "name": "华勤技术"},
                  {"code": "603259.SH", "name": "药明康德"},
                  {"code": "688981.SH", "name": "中芯国际"}]
        attach_hk_twins(sample, lambda n: fake[n])
        assert sample[0].get("hk_code") is None
        assert sample[1].get("hk_code") is None
        assert sample[2]["hk_code"] == "02359.HK"
        assert sample[3].get("hk_code") is None
        # 解析异常只告警，不影响其余标的
        def boom(_name):
            raise OSError("network down")
        sample2 = [{"code": "600233.SH", "name": "圆通速递"}]
        attach_hk_twins(sample2, boom)
        assert sample2[0].get("hk_code") is None
        print("selftest PASS")
        return

    as_of, product_order, stocks = load_pool(args.excel_path)
    pool = build_pool(as_of, product_order, stocks)
    attach_hk_twins(pool["stocks"], lambda n: match_hk_twin(fetch_suggest(n), n))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(pool, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"已生成：{args.output.resolve()}（{pool['count']} 只标的，"
          f"{sum(1 for s in pool['stocks'] if s.get('hk_code'))} 只挂港股孪生）")


if __name__ == "__main__":
    main()
