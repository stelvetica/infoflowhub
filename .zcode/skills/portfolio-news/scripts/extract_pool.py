"""从持仓 Excel 抽取标的池，输出 JSON。

职责（确定性，AI 不参与）：
- 读 Excel 固定列（col 0 产品名 / col 1 代码 / col 2 名称 / col 6 仓位）。
- 去重得到股票列表，按最高单产品仓位降序。
- 仅保留至少一个产品仓位 > 0 的股票。
- 输出 JSON：每只股票含 code/name/holders/max_weight。

不承担：行业/产业链/主营业务归类（由阶段二 AI 维护标的信息库）。

路径：默认读 C:/Users/ETT/Playground/infoflowhub/data/标的观察/持仓标的.xlsx，输出到主 checkout 的
infoflowhub/data/reports/标的池.json（产物根）。
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

DEFAULT_EXCEL_PATH = Path(r"C:\Users\ETT\Playground\infoflowhub\data\标的观察\持仓标的.xlsx")
DEFAULT_OUTPUT_PATH = Path(r"C:\Users\ETT\Playground\infoflowhub\data\reports\标的池.json")


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
    as_of, product_order, stocks = load_pool(args.excel_path)
    pool = build_pool(as_of, product_order, stocks)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(pool, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"已生成：{args.output.resolve()}（{pool['count']} 只标的）")


if __name__ == "__main__":
    main()
