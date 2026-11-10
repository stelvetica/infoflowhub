"""market-hotspot 脚本离线单测：注入固定时钟 / fake fetcher，不触网。"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "skills" / "market-hotspot" / "scripts" / "collect_hotspot.py"
SPEC = importlib.util.spec_from_file_location("collect_hotspot", MODULE_PATH)
assert SPEC and SPEC.loader
hotspot = importlib.util.module_from_spec(SPEC)
sys.modules["collect_hotspot"] = hotspot
SPEC.loader.exec_module(hotspot)

WINDOW = hotspot.Window("2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", False)


def bar(day: str, close: float, amount: float = 20000.0, volume: float = 20000.0, precise: bool = True) -> list[Any]:
    item: list[Any] = [day, str(close), str(close), str(close), str(close), str(volume), {}]
    if precise:
        item.extend([str(1.0), str(amount), ""])
    return item


def business_days(count: int, end: date = date(2026, 11, 6)) -> list[date]:
    days: list[date] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


def stock_bars(count: int = 60, close: float = 10.0) -> list[list[Any]]:
    return [bar(day.isoformat(), close) for day in business_days(count)]


# ── 窗口 / 涨幅 ──────────────────────────────────────────────


def test_window_switch_at_1505():
    days = [day.isoformat() for day in business_days(20)] + ["2026-11-09"]
    bench = [bar(day, 100 + index) for index, day in enumerate(days)]
    early = hotspot.compute_window(bench, datetime(2026, 11, 9, 15, 4, 59))
    assert (early.d0, early.d1, early.d2, early.d3) == ("2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06")
    assert early.include_today is False
    late = hotspot.compute_window(bench, datetime(2026, 11, 9, 15, 5, 0))
    assert (late.d1, late.d2, late.d3) == ("2026-11-05", "2026-11-06", "2026-11-09")
    assert late.include_today is True


def test_window_ignores_future_bars():
    bars = [bar("2026-11-03", 1), bar("2026-11-04", 1), bar("2026-11-05", 1), bar("2026-11-06", 1), bar("2026-11-10", 1)]
    window = hotspot.compute_window(bars, datetime(2026, 11, 9, 12, 0, 0))
    assert window.d3 == "2026-11-06"


def test_r3_calculation():
    assert hotspot.pct_change(100.0, 110.0) == pytest.approx(0.1)
    assert hotspot.pct_change(0.0, 110.0) is None
    assert hotspot.pct_change(100.0, None) is None


# ── 行业排序 ────────────────────────────────────────────────


def test_rank_ties_by_amount_then_code():
    records = [
        {"code": "pt2", "name": "B", "valid": True, "r3": 0.1, "amount_avg_wan": 100.0},
        {"code": "pt1", "name": "A", "valid": True, "r3": 0.1, "amount_avg_wan": 200.0},
        {"code": "pt3", "name": "C", "valid": True, "r3": 0.1, "amount_avg_wan": 200.0},
        {"code": "pt0", "name": "X", "valid": False},
    ]
    assert [item["code"] for item in hotspot.rank_sector_records(records)] == ["pt1", "pt3", "pt2"]


def test_top5_limit():
    records = [{"code": f"pt{i}", "name": str(i), "valid": True, "r3": i / 100, "amount_avg_wan": 1.0} for i in range(8)]
    assert len(hotspot.top_sectors(records, 5)) == 5
    assert hotspot.top_sectors(records, 5)[0]["code"] == "pt7"


# ── 广度 ───────────────────────────────────────────────────


def test_breadth_boundaries():
    breadth = hotspot.compute_breadth([0.01, 0.02, 0.03], 0.04, strong_count=3)
    assert breadth["breadth_ratio"] == 1.0
    assert breadth["weight_driven"] is True
    assert breadth["narrow_breadth"] is False
    breadth = hotspot.compute_breadth([0.05, -0.01, -0.02], 0.0, strong_count=1)
    assert breadth["breadth_ratio"] == pytest.approx(1 / 3)
    assert breadth["narrow_breadth"] is False
    breadth = hotspot.compute_breadth([-0.01, -0.02, -0.03, -0.04], 0.0, strong_count=0)
    assert breadth["no_strong"] is True
    assert breadth["narrow_breadth"] is True


# ── 股票过滤 ───────────────────────────────────────────────


def test_filter_non_a_delisted_st_bj():
    bars = stock_bars()
    assert hotspot.classify_stock("sh900001", "B股", bars, WINDOW, 0.0)["excluded_reason"] == "non_a_share"
    assert hotspot.classify_stock("bj430047", "北交所", bars, WINDOW, 0.0)["excluded_reason"] == "non_a_share"
    assert hotspot.classify_stock("sh600000", "退市测试", bars, WINDOW, 0.0)["excluded_reason"] == "delisted"
    assert hotspot.classify_stock("sh600000", "*ST测试", bars, WINDOW, 0.0)["excluded_reason"] == "st"
    assert hotspot.classify_stock("sh600000", "SST测试", bars, WINDOW, 0.0)["excluded_reason"] == "st"
    assert hotspot.classify_stock("sh600000", "PT测试", bars, WINDOW, 0.0)["excluded_reason"] == "st"


def test_filter_59_vs_60_bars():
    assert hotspot.classify_stock("sh600000", "测试", stock_bars(59), WINDOW, 0.0)["excluded_reason"] == "listed_short"
    assert hotspot.classify_stock("sh600000", "测试", stock_bars(60), WINDOW, 0.0)["excluded_reason"] is None


def test_filter_suspended_and_zero_amount():
    halted = stock_bars(59)  # 最后一根停在 D2
    assert hotspot.classify_stock("sh600000", "测试", halted, WINDOW, 0.0)["excluded_reason"] == "listed_short"
    # 60 根但最后一根停在 D2（11-05），触发“最后 bar ≠ D3”
    halted_days = business_days(61)[:60]
    assert halted_days[-1].isoformat() == "2026-11-05"
    halted_60 = [bar(day.isoformat(), 10.0) for day in halted_days]
    assert hotspot.classify_stock("sh600000", "测试", halted_60, WINDOW, 0.0)["excluded_reason"] == "suspended"

    zero = stock_bars(60)
    zero[-1] = bar("2026-11-06", 10.0, amount=0.0)
    assert hotspot.classify_stock("sh600000", "测试", zero, WINDOW, 0.0)["excluded_reason"] == "suspended"


def test_amount_threshold_boundary():
    low = [bar(day.isoformat(), 10.0, amount=4999.99) for day in business_days(60)]
    ok = [bar(day.isoformat(), 10.0, amount=5000.0) for day in business_days(60)]
    assert hotspot.classify_stock("sh600000", "测试", low, WINDOW, 0.0)["excluded_reason"] == "low_amount"
    result = hotspot.classify_stock("sh600000", "测试", ok, WINDOW, 0.0)
    assert result["excluded_reason"] is None
    assert result["r3"] == 0.0
    assert result["strong"] is False


def test_688_estimated_amount_unit():
    main_board = bar("2026-11-06", 10.0, precise=False, volume=10000.0)
    star = bar("2026-11-06", 10.0, precise=False, volume=10000.0)
    assert hotspot.estimate_amount_wan("sh600000", main_board) == pytest.approx(1000.0)
    assert hotspot.estimate_amount_wan("sh688001", star) == pytest.approx(10.0)


def test_stock_counts_identity():
    results = [
        {"code": "sh600000", "name": "A", "excluded_reason": None, "r3": 0.05},
        {"code": "sh600001", "name": "B", "excluded_reason": None, "r3": 0.01},
        {"code": "sh600002", "name": "C", "excluded_reason": "st"},
        {"code": "sh900001", "name": "D", "excluded_reason": "non_a_share"},
        {"code": "sh600003", "name": "E", "excluded_reason": "low_amount"},
    ]
    summary = hotspot.summarize_stock_results(results, 0.02)
    counts = summary["counts"]
    assert counts == {
        "raw": 5,
        "universe_eligible": 3,
        "data_eligible": 2,
        "strong": 1,
        "below_benchmark": 1,
    }


# ── 科创板对齐 / 缓存 ──────────────────────────────────────


def test_kcb_alignment_and_override():
    names = {"半导体", "IT服务Ⅱ", "专用设备", "塑料"}
    assert hotspot.align_kcb_industry("sh688001", "IT服务", names, {}) == "IT服务Ⅱ"
    assert hotspot.align_kcb_industry("sh688001", "塑料制品", names, {}) == "塑料"
    assert hotspot.align_kcb_industry("sh688001", "军工装备", names, {}) is None
    assert hotspot.align_kcb_industry("sh688001", "军工装备", names, {"sh688001": "专用设备"}) == "专用设备"
    assert hotspot.align_kcb_industry("sh688001", "军工装备", names, {"sh688001": "不存在"}) is None


def test_kcb_cache_ttl():
    now = datetime(2026, 11, 9, 12, 0, 0)
    assert hotspot.kcb_cache_is_fresh({"fetched_at": "2026-11-03T12:00:00"}, now) is True
    assert hotspot.kcb_cache_is_fresh({"fetched_at": "2026-11-02T11:59:59"}, now) is False
    assert hotspot.kcb_cache_is_fresh(None, now) is False


class _DummyResponse:
    def __init__(self, status: int, payload: Any):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> Any:
        return self._payload


class _XueqiuSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.home_calls = 0
        self.industry_calls = 0

    def get(self, url: str, **_kwargs: Any) -> _DummyResponse:
        if url == "https://www.xueqiu.com/":
            self.home_calls += 1
            return _DummyResponse(200, {})
        self.industry_calls += 1
        if self.industry_calls == 1:
            return _DummyResponse(400, {"error_code": 400016, "error_description": "token"})
        return _DummyResponse(200, {"data": {"industry": [{"ind_name": "半导体"}]}})


def test_xueqiu_400016_rebuilds_session(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(hotspot.time, "sleep", lambda _seconds: None)
    session = _XueqiuSession()
    fetcher = hotspot.HttpFetcher(session)
    assert fetcher.xueqiu_industry("SH688001") == "半导体"
    assert session.home_calls == 2


def test_kcb_uses_stale_cache_when_xueqiu_fails(tmp_path: Path):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(
        json.dumps({"fetched_at": "2020-01-01T00:00:00", "industries": {"sh688001": "半导体"}}),
        encoding="utf-8",
    )

    class FailingFetcher:
        def xueqiu_industry(self, _code: str) -> str:
            raise hotspot.FetchError("boom")

    info = hotspot.get_kcb_industry(
        FailingFetcher(),
        [{"code": "sh688001", "name": "科创甲"}],
        datetime(2026, 11, 9, 12, 0, 0),
        cache_path,
        {},
        {"半导体"},
        write_cache=False,
    )
    assert info["source"] == "cache_stale"
    assert info["map"]["sh688001"] == "半导体"


# ── 渲染 / 主流程 ──────────────────────────────────────────


def _sample_report() -> dict[str, Any]:
    return {
        "report_date": "2026-11-09",
        "generated_at": "2026-11-09T12:00:00",
        "window": {
            "base_date": WINDOW.d0,
            "trading_days": WINDOW.trading_days,
            "end_date": WINDOW.d3,
            "include_today": False,
            "benchmark_r3": 0.01,
        },
        "top_industries": [
            {
                "rank": 1,
                "code": "pt01801081",
                "name": "半导体",
                "r3": 0.05,
                "excess": 0.04,
                "amount_avg_wan": 1000.0,
                "breadth": {
                    "median": 0.02,
                    "strong_count": 2,
                    "breadth_ratio": 0.4,
                    "weight_driven": False,
                    "narrow_breadth": False,
                    "no_strong": False,
                },
                "counts": {"raw": 5, "universe_eligible": 5, "data_eligible": 5, "strong": 2, "below_benchmark": 3},
                "stocks": [{"name": "甲", "r3": 0.08}],
                "excluded_counts": {},
                "excluded_examples": [],
                "constituent_status": {"incomplete": False},
            }
        ],
        "top_sw1": [],
        "industry_rankings": {
            "sw2": {"valid_count": 1, "expected_count": 124, "ranking": [], "excluded": []},
            "sw1": {"valid_count": 0, "expected_count": 31, "ranking": [], "excluded": []},
        },
        "data_quality": {"issues": []},
        "universe": {
            "raw": 5,
            "universe_eligible": 5,
            "data_eligible": 5,
            "strong": 2,
            "below_benchmark": 3,
            "excluded_counts": {},
        },
    }


def test_render_markdown_structure_and_placeholders():
    markdown = hotspot.render_markdown(_sample_report())
    assert markdown.startswith("# 市场热点 2026-11-09")
    for heading in ("## 结论速览", "## 口径", "## 申万二级 Top 5（主表）", "## 原因分析（申万二级 Top 5）", "## 申万一级 Top 5（对照）", "## 全量概况", "## 数据质量"):
        assert heading in markdown
    assert "<details>" not in markdown
    assert markdown.count("<!-- AI 填写 -->") == 2


def test_verify_counts_identity():
    hotspot.verify_counts_identity(_sample_report())
    broken = _sample_report()
    broken["universe"]["strong"] = 4
    with pytest.raises(AssertionError):
        hotspot.verify_counts_identity(broken)


class FakeFetcher:
    def __init__(self) -> None:
        self.catalogs = {
            "hy2": [{"code": "ptA", "name": "半导体"}, {"code": "ptB", "name": "白酒Ⅱ"}],
            "hy": [{"code": "pt01", "name": "电子"}, {"code": "pt02", "name": "食品饮料"}],
        }
        self.members = {
            "ptA": [{"code": "sh600001", "name": "甲公司"}, {"code": "sz000001", "name": "乙公司"}],
            "ptB": [{"code": "sh600002", "name": "丙公司"}],
        }
        self.kcb = [{"symbol": "sh688001", "name": "科创甲"}]
        self.kcb_industries = {"sh688001": "半导体"}
        self.stock_bars = {
            "sh600001": stock_bars(60, 10.0),
            "sz000001": stock_bars(60, 12.0),
            "sh600002": stock_bars(60, 9.0),
            "sh688001": stock_bars(60, 15.0),
        }
        self.sector_bars = {
            "ptA": [bar(day.isoformat(), 100.0 + index) for index, day in enumerate(business_days(20))],
            "ptB": [bar(day.isoformat(), 100.0 - index) for index, day in enumerate(business_days(20))],
            "pt01": [bar(day.isoformat(), 100.0 + index) for index, day in enumerate(business_days(20))],
            "pt02": [bar(day.isoformat(), 100.0) for day in business_days(20)],
        }
        self.benchmark = [bar(day.isoformat(), 3000.0 + index) for index, day in enumerate(business_days(20))]

    def get_json(self, url: str) -> Any:
        if "getRank" in url:
            board_type = re.search(r"board_type=(\w+)", url).group(1)
            return {"data": {"rank_list": self.catalogs[board_type]}}
        if "newfqkline" in url:
            code = re.search(r"param=([^,]+),", url).group(1)
            if code == "sh000001":
                return {"data": {code: {"day": self.benchmark}}}
            bars = self.sector_bars.get(code) or self.stock_bars[code]
            key = "qfqday" if code.startswith(("sh6", "sz0", "sz3")) else "day"
            return {"data": {code: {key: bars}}}
        if "getBoardRankList" in url:
            code = re.search(r"board_code=([^&]+)", url).group(1)
            offset = int(re.search(r"offset=(\d+)", url).group(1))
            rows = self.members.get(code, [])
            return {"data": {"total": len(rows), "rank_list": rows[offset : offset + 200]}}
        if "Market_Center" in url:
            page = int(re.search(r"page=(\d+)", url).group(1))
            return self.kcb if page == 1 else []
        raise AssertionError(f"unexpected url: {url}")

    def xueqiu_industry(self, code: str) -> str | None:
        return self.kcb_industries.get(code)


def test_dry_run_writes_nothing(tmp_path: Path):
    reports = tmp_path / "data" / "reports"
    rc = hotspot.main(
        ["--dry-run"],
        now=datetime(2026, 11, 9, 12, 0, 0),
        fetcher=FakeFetcher(),
        reports_dir=reports,
    )
    assert rc == 0
    assert not reports.exists() or list(reports.rglob("*")) == []


def test_fatal_when_benchmark_missing(tmp_path: Path):
    class FailingFetcher:
        def get_json(self, _url: str) -> Any:
            raise hotspot.FetchError("network down")

    rc = hotspot.main(
        [],
        now=datetime(2026, 11, 9, 12, 0, 0),
        fetcher=FailingFetcher(),
        reports_dir=tmp_path / "reports",
    )
    assert rc == 2
    assert not (tmp_path / "reports").exists()


def test_normal_run_writes_md_and_json(tmp_path: Path):
    reports = tmp_path / "reports"
    rc = hotspot.main(
        [],
        now=datetime(2026, 11, 9, 12, 0, 0),
        fetcher=FakeFetcher(),
        reports_dir=reports,
    )
    assert rc == 0
    markdown = (reports / "市场热点_2026-11-09.md").read_text(encoding="utf-8")
    payload = json.loads((reports / "市场热点底稿_2026-11-09.json").read_text(encoding="utf-8"))
    assert markdown.startswith("# 市场热点 2026-11-09")
    assert payload["schema_version"] == 1
    assert payload["window"]["end_date"] == "2026-11-06"
    assert payload["sources"]["eastmoney_used"] is False
    assert payload["universe"]["raw"] == payload["universe"]["universe_eligible"] + payload["universe"]["excluded_counts"].get("st", 0) + payload["universe"]["excluded_counts"].get("non_a_share", 0) + payload["universe"]["excluded_counts"].get("delisted", 0)


def test_selftest_exit_zero(capsys: pytest.CaptureFixture[str]):
    assert hotspot.selftest() == 0
    assert "SELFTEST PASS" in capsys.readouterr().out
