"""操作分析报告结构：解读在前、四段数据由脚本出、只留各产品点评给 AI。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "portfolio-operations" / "scripts" / "collect_operations.py"
_spec = importlib.util.spec_from_file_location("collect_operations", SCRIPT)
ops = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ops)

CFG = {
    "top_n": 3,
    "trades_csv": "历史成交汇总.csv",
    "valuation_root": "C:/预估值_PB系统/产品",
    "windows": {"week": 5, "month": 20},
}


def _window(net: int, sell: int, stocks: list[dict], industries: dict[str, int]) -> dict:
    return {"start": "20261030", "end": "20261105", "buy": net + sell, "sell": sell,
            "net": net, "repo": 0, "stocks": stocks, "industries": industries, "unresolved": []}


def _product(name: str, nav: float, week: dict, month: dict, *, note: str = "", own_nav=None, swap: float = 0.0) -> dict:
    return {
        "name": name, "nav": nav, "note": note, "own_nav": own_nav,
        "position": {"date": "20261104", "equity": 69.5, "stock": 69.5 - swap, "swap": swap},
        "week": week, "month": month,
    }


def _report() -> dict:
    stock_a = {"code": "1", "name": "甲", "industry": "医药", "buy": 0, "sell": 15_000_000, "net": -15_000_000}
    stock_b = {"code": "2", "name": "乙", "industry": "电子", "buy": 0, "sell": 10_000_000, "net": -10_000_000}
    stock_c = {"code": "3", "name": "丙", "industry": "设备", "buy": 5_000_000, "sell": 0, "net": 5_000_000}
    etf = {"code": "510300.SH", "name": "沪深300ETF", "industry": "ETF", "buy": 0, "sell": 2_000_000, "net": -2_000_000}
    p1 = _product(
        "A", 1e8,
        _window(-22_000_000, 27_000_000, [stock_a, stock_b, stock_c, etf],
                {"医药": -15_000_000, "电子": -10_000_000, "设备": 5_000_000, "ETF": -2_000_000}),
        _window(-10_000_000, 15_000_000,
                [{"code": "1", "name": "甲", "industry": "医药", "buy": 12_000_000, "sell": 0, "net": 12_000_000}],
                {"医药": 12_000_000, "电子": -22_000_000}),
        swap=20.5,
    )
    p2 = _product(
        "B", 1.45e8,
        _window(3_000_000, 4_000_000,
                [{"code": "3", "name": "丙", "industry": "设备", "buy": 4_000_000, "sell": 1_000_000, "net": 3_000_000}],
                {"设备": 3_000_000}),
        _window(8_000_000, 9_000_000,
                [{"code": "3", "name": "丙", "industry": "设备", "buy": 9_000_000, "sell": 1_000_000, "net": 8_000_000}],
                {"设备": 8_000_000}),
        note="FOF", own_nav=5_910_000,
    )
    return {"date_iso": "2026-11-05", "trade_end": "20261105", "valuation_end": "20261104",
            "windows": {"week": ("20261030", "20261105"), "month": ("20261009", "20261105")},
            "products": [p1, p2], "total": {}}


def test_report_structure_and_sections():
    md = ops.render_md(_report(), CFG)
    assert md.index("## 解读") < md.index("## 口径")
    for title in ("### 1. 个股动向", "### 2. 行业动向", "### 3. 交易特征",
                  "### 4. 产品分化", "### 5. 各产品点评"):
        assert title in md, title
    assert "- **A**：<!-- AI 填写 -->" in md
    assert "- **B**：<!-- AI 填写 -->" in md


def test_report_has_no_cross_product_total():
    md = ops.render_md(_report(), CFG)
    assert "全部产品合计" not in md
    assert "| **合计** |" not in md


def test_caliber_block_is_short():
    md = ops.render_md(_report(), CFG)
    block = md.split("## 口径", 1)[1].split("## 总览", 1)[0]
    bullets = [line for line in block.splitlines() if line.startswith("- ")]
    assert len(bullets) <= 3, bullets
    assert any("FOF" in line or "母基金" in line for line in bullets)
    assert any("互换" in line for line in bullets)


def test_industry_shift_marks_reversal():
    assert "转为减仓" in ops.industry_shift_cell(_report()["products"][0])
