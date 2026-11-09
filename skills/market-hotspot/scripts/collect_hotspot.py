"""市场热点分析：近 3 个交易日行业（申万二级 / 一级）涨幅 + 强势股底稿。

职责（确定性）：
- 用上证指数日 K 推导最近 3 个完整交易日（15:05 前剔除当日盘中 bar）。
- 抓申万二级（124）与一级（31）板块指数日 K，按 3 日涨幅排序，取前 5。
- 抓前 5 二级行业的成分股（腾讯）+ 科创板（新浪代码 / 雪球行业），逐股抓前复权日 K，
  过滤 ST / 退市 / 停牌 / 上市不足 60 根 / 日均成交额 < 5000 万，产出强势股与广度。
- 写出 MD（AI 原因占位）与 JSON 底稿；不写任何 AI 结论。

用法：
    python collect_hotspot.py              # 抓取 + 计算 + 写文件
    python collect_hotspot.py --dry-run    # 抓取 + 计算 + 打印摘要，不写文件
    python collect_hotspot.py --selftest   # 纯函数自检，不触网不落盘

数据源只用腾讯 / 新浪 / 雪球。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from infra.utf8_json import dump_json_utf8  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parents[1]
REPORTS_DIR = Path(r"C:\Users\ETT\Playground\infoflowhub\data\reports")
CACHE_RELATIVE = Path(".cache") / "kcb_industry.json"
OVERRIDES_PATH = SKILL_DIR / "references" / "kcb_industry_overrides.json"

BENCHMARK_CODE = "sh000001"
CUTOFF = dtime(15, 5)
KCB_TTL_DAYS = 7
AMOUNT_THRESHOLD_WAN = 5000.0
AVG_AMOUNT_DAYS = 3  # D1..D3
WORKERS = 8
RETRIES = 3
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
XUEQIU_HOME = "https://www.xueqiu.com/"
# SW2014（雪球）→ 腾讯申万二级（SW2021）无歧义改名；其余含糊名称统一“未归类”。
KCB_ALIASES = {
    "塑料制品": "塑料",
    "汽车服务及其他": "汽车服务",
}
class FatalError(Exception):
    """不可降级的失败：退出 2，且不覆盖旧产物。"""


class FetchError(Exception):
    pass


# ── 窗口与基础计算 ────────────────────────────────────────────


@dataclass(frozen=True)
class Window:
    d0: str
    d1: str
    d2: str
    d3: str
    include_today: bool

    @property
    def trading_days(self) -> list[str]:
        return [self.d1, self.d2, self.d3]


def bar_date(bar: list[Any]) -> str:
    return str(bar[0])


def to_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def bar_close(bar: list[Any]) -> float | None:
    return to_float(bar[2]) if len(bar) > 2 else None


def bar_volume(bar: list[Any]) -> float | None:
    return to_float(bar[5]) if len(bar) > 5 else None


def bar_amount_precise(bar: list[Any]) -> float | None:
    """腾讯 K 线第 8 列（0-based）为成交额，单位万元；缺失或非法返回 None。"""
    if len(bar) <= 8:
        return None
    value = to_float(bar[8])
    if value is None or value < 0:
        return None
    return value


def is_star_code(code: str) -> bool:
    return code.startswith("sh688") or code.startswith("sh689")


def estimate_amount_wan(code: str, bar: list[Any]) -> float | None:
    """按 volume×close 估算成交额（万元）：688/689 volume 为股，其余为手。"""
    volume = bar_volume(bar)
    close = bar_close(bar)
    if volume is None or close is None or volume <= 0 or close <= 0:
        return None
    if is_star_code(code):
        return volume * close / 10000.0
    return volume * close / 100.0


def bar_amount_wan(code: str, bar: list[Any]) -> tuple[float | None, str]:
    precise = bar_amount_precise(bar)
    if precise is not None:
        return precise, "precise"
    estimated = estimate_amount_wan(code, bar)
    if estimated is None:
        return None, "missing"
    return estimated, "estimated"


def dedupe_sorted_bars(bars: Iterable[list[Any]], end_date: str | None = None) -> list[list[Any]]:
    seen: set[str] = set()
    result: list[list[Any]] = []
    for bar in sorted(bars, key=bar_date):
        if not isinstance(bar, (list, tuple)) or len(bar) < 6:
            continue
        day = bar_date(bar)
        if end_date is not None and day > end_date:
            continue
        if day in seen:
            continue
        seen.add(day)
        result.append(list(bar))
    return result


def compute_window(bench_bars: Iterable[list[Any]], now: datetime) -> Window:
    """最后 4 根有效基准 bar = D0..D3；15:05 前剔除当日盘中 bar。"""
    today = now.date().isoformat()
    bars = dedupe_sorted_bars(bench_bars, end_date=today)
    if not bars:
        raise ValueError("基准指数无有效日K")
    if bar_date(bars[-1]) == today and now.time() < CUTOFF:
        bars = bars[:-1]
    if len(bars) < 4:
        raise ValueError("基准指数有效日K不足 4 根")
    tail = bars[-4:]
    dates = [bar_date(bar) for bar in tail]
    if len(set(dates)) != 4 or dates != sorted(dates):
        raise ValueError("基准指数最后 4 根日期不严格递增")
    return Window(d0=dates[0], d1=dates[1], d2=dates[2], d3=dates[3], include_today=dates[3] == today)


def pct_change(close_start: float, close_end: float) -> float | None:
    if close_start is None or close_end is None or close_start <= 0:
        return None
    return close_end / close_start - 1.0


def closes_for_window(bars: list[list[Any]], window: Window) -> dict[str, float] | None:
    by_date: dict[str, float] = {}
    for bar in bars:
        close = bar_close(bar)
        if close is None or close <= 0:
            continue
        by_date[bar_date(bar)] = close
    try:
        return {day: by_date[day] for day in (window.d0, window.d1, window.d2, window.d3)}
    except KeyError:
        return None


def series_matches_window(bars: list[list[Any]], window: Window) -> bool:
    filtered = dedupe_sorted_bars(bars, end_date=window.d3)
    return len(filtered) >= 4 and [bar_date(bar) for bar in filtered[-4:]] == [
        window.d0,
        window.d1,
        window.d2,
        window.d3,
    ]


def avg_amount_wan(code: str, bars: list[list[Any]], window: Window) -> tuple[float | None, str]:
    """D1..D3 日均成交额（万元）；任一缺失则整体缺失。"""
    by_date = {bar_date(bar): bar for bar in bars}
    total = 0.0
    sources: set[str] = set()
    for day in (window.d1, window.d2, window.d3):
        bar = by_date.get(day)
        if bar is None:
            return None, "missing"
        amount, source = bar_amount_wan(code, bar)
        if amount is None:
            return None, "missing"
        total += amount
        sources.add(source)
    return total / AVG_AMOUNT_DAYS, ("estimated" if "estimated" in sources else "precise")


# ── 排序 / 广度 ───────────────────────────────────────────────


def rank_sector_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    valid = [record for record in records if record.get("valid")]
    valid.sort(key=lambda item: (-item["r3"], -(item.get("amount_avg_wan") or 0.0), item["code"]))
    return valid


def top_sectors(records: Iterable[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    return rank_sector_records(records)[:limit]


def compute_breadth(
    stock_r3: list[float], industry_r3: float, strong_count: int | None = None
) -> dict[str, Any]:
    eligible = len(stock_r3)
    if strong_count is None:
        strong_count = sum(1 for value in stock_r3 if value > 0)
    median = statistics.median(stock_r3) if stock_r3 else None
    breadth_ratio = (strong_count / eligible) if eligible else 0.0
    weight_driven = median is not None and median < industry_r3 - 0.005
    narrow_breadth = breadth_ratio < 0.30
    return {
        "median": round(median, 6) if median is not None else None,
        "strong_count": strong_count,
        "breadth_ratio": round(breadth_ratio, 6),
        "weight_driven": bool(weight_driven),
        "narrow_breadth": bool(narrow_breadth),
        "no_strong": strong_count == 0,
    }


# ── 股票过滤 ─────────────────────────────────────────────────


def is_a_share_code(code: str) -> bool:
    if re.fullmatch(r"sh(600|601|603|605|688|689)\d{3}", code):
        return True
    if re.fullmatch(r"sz(000|001|002|003|300|301)\d{3}", code):
        return True
    return False


def is_st_name(name: str) -> bool:
    upper = (name or "").strip().upper().replace(" ", "")
    return upper.startswith(("*ST", "ST", "S*ST", "SST", "PT"))


def classify_stock(
    code: str,
    name: str,
    bars: list[list[Any]],
    window: Window,
    bench_r3: float,
) -> dict[str, Any]:
    """按固定顺序过滤：一股只记一个原因。返回完整结果（eligible 或 excluded）。"""
    result: dict[str, Any] = {"code": code, "name": name, "excluded_reason": None}
    bars = dedupe_sorted_bars(bars, end_date=window.d3)
    if not is_a_share_code(code):
        result["excluded_reason"] = "non_a_share"
        return result
    if "退" in (name or ""):
        result["excluded_reason"] = "delisted"
        return result
    if is_st_name(name):
        result["excluded_reason"] = "st"
        return result

    valid_bars = [
        bar
        for bar in bars
        if bar_date(bar) <= window.d3 and bar_close(bar) is not None and bar_close(bar) > 0
    ]
    if len(valid_bars) < 60:
        result["excluded_reason"] = "listed_short"
        return result
    if not series_matches_window(bars, window):
        result["excluded_reason"] = "suspended"
        return result
    by_date = {bar_date(bar): bar for bar in bars}
    d3_bar = by_date.get(window.d3)
    if d3_bar is None:
        result["excluded_reason"] = "suspended"
        return result
    d3_amount = bar_amount_precise(d3_bar)
    if d3_amount is not None and d3_amount <= 0:
        result["excluded_reason"] = "suspended"
        return result

    closes = closes_for_window(bars, window)
    if closes is None or any(value <= 0 for value in closes.values()):
        result["excluded_reason"] = "bad_price"
        return result
    r3 = pct_change(closes[window.d0], closes[window.d3])
    if r3 is None:
        result["excluded_reason"] = "bad_price"
        return result

    amount_avg, amount_source = avg_amount_wan(code, bars, window)
    if amount_avg is None:
        result["excluded_reason"] = "amount_missing"
        return result
    if amount_avg < AMOUNT_THRESHOLD_WAN:
        result["excluded_reason"] = "low_amount"
        return result

    result.update(
        {
            "excluded_reason": None,
            "r3": round(r3, 6),
            "excess": round(r3 - bench_r3, 6),
            "amount_avg_wan": round(amount_avg, 2),
            "amount_source": amount_source,
            "absolute_up": r3 > 0,
            "strong": r3 > bench_r3,
        }
    )
    return result


def summarize_stock_results(results: Iterable[dict[str, Any]], bench_r3: float) -> dict[str, Any]:
    results = list(results)
    excluded_counts: dict[str, int] = {}
    excluded_examples: list[dict[str, str]] = []
    for item in results:
        reason = item.get("excluded_reason")
        if not reason:
            continue
        excluded_counts[reason] = excluded_counts.get(reason, 0) + 1
        if len(excluded_examples) < 12:
            excluded_examples.append({"code": item["code"], "name": item["name"], "reason": reason})
    universe_eligible = sum(
        1 for item in results if item.get("excluded_reason") not in ("non_a_share", "delisted", "st")
    )
    eligible = [item for item in results if not item.get("excluded_reason")]
    strong = [item for item in eligible if item["r3"] > bench_r3]
    counts = {
        "raw": len(results),
        "universe_eligible": universe_eligible,
        "data_eligible": len(eligible),
        "strong": len(strong),
        "below_benchmark": len(eligible) - len(strong),
    }
    return {
        "counts": counts,
        "eligible": sorted(eligible, key=lambda item: (-item["r3"], item["code"])),
        "strong": sorted(strong, key=lambda item: (-item["r3"], item["code"])),
        "excluded_counts": excluded_counts,
        "excluded_examples": excluded_examples,
    }


# ── 科创板行业对齐 / 缓存 ────────────────────────────────────


def align_kcb_industry(
    code: str,
    raw_name: str | None,
    catalog_names: set[str],
    overrides: dict[str, str],
) -> str | None:
    override = overrides.get(code)
    if override:
        return override if override in catalog_names else None
    if not raw_name:
        return None
    if raw_name in catalog_names:
        return raw_name
    if raw_name + "Ⅱ" in catalog_names:
        return raw_name + "Ⅱ"
    alias = KCB_ALIASES.get(raw_name)
    if alias and alias in catalog_names:
        return alias
    return None


def parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def kcb_cache_is_fresh(cache: dict[str, Any] | None, now: datetime, ttl_days: int = KCB_TTL_DAYS) -> bool:
    if not isinstance(cache, dict):
        return False
    fetched_at = parse_iso_datetime(cache.get("fetched_at"))
    if fetched_at is None:
        return False
    return timedelta(0) <= now - fetched_at < timedelta(days=ttl_days)


def load_json_file(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return fallback


def load_overrides(path: Path = OVERRIDES_PATH) -> dict[str, str]:
    data = load_json_file(path, {})
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items() if isinstance(value, str)}


# ── 网络层 ───────────────────────────────────────────────────


def kline_url(code: str, count: int) -> str:
    return (
        "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
        f"?param={code},day,,,{count},qfq"
    )


def rank_url(board_type: str) -> str:
    return (
        "https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank"
        f"?board_type={board_type}&sort_type=price&direct=down&offset=0&count=200&app=wzq"
    )


def board_url(code: str, offset: int) -> str:
    return (
        "https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList"
        f"?board_code={code}&sort_type=price&direct=down&offset={offset}&count=200"
    )


def kcb_page_url(page: int) -> str:
    return (
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
        f"?page={page}&num=100&sort=symbol&asc=1&node=kcb&symbol=&_s_r_a=page"
    )


def xueqiu_url(code: str) -> str:
    return f"https://stock.xueqiu.com/v5/stock/f10/cn/industry.json?symbol={code.upper()}"


class HttpFetcher:
    """requests 会话封装：3 次退避重试 + 雪球 cookie 初始化/400016 重试。"""

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self._xq_ready = False
        self._lock = threading.Lock()

    def _sleep(self, attempt: int) -> None:
        time.sleep(0.5 * (2**attempt))

    def get_json(self, url: str) -> Any:
        last: Exception | None = None
        for attempt in range(RETRIES):
            try:
                response = self.session.get(url, timeout=20)
                response.raise_for_status()
                return response.json()
            except Exception as exc:  # noqa: BLE001 - 网络异常统一重试
                last = exc
                if attempt < RETRIES - 1:
                    self._sleep(attempt)
        raise FetchError(f"请求失败: {url} ({last})")

    def _ensure_xueqiu(self) -> None:
        with self._lock:
            if self._xq_ready:
                return
            try:
                self.session.get(XUEQIU_HOME, timeout=20, allow_redirects=True)
            except Exception as exc:  # noqa: BLE001
                raise FetchError(f"雪球首页 cookie 初始化失败: {exc}") from exc
            self._xq_ready = True

    def xueqiu_industry(self, code: str) -> str | None:
        self._ensure_xueqiu()
        last: Exception | None = None
        for attempt in range(2):
            try:
                response = self.session.get(xueqiu_url(code), timeout=20)
                if response.status_code == 200:
                    payload = response.json()
                    data = payload.get("data") or {}
                    industries = data.get("industry") or []
                    if industries:
                        return industries[0].get("ind_name")
                    return None
                if response.status_code in (400, 401, 403) or "400016" in response.text:
                    with self._lock:
                        self._xq_ready = False
                    self._ensure_xueqiu()
                    last = FetchError(f"雪球 token 失效: {code}")
                    continue
                last = FetchError(f"雪球返回 {response.status_code}: {code}")
            except Exception as exc:  # noqa: BLE001
                last = exc
            if attempt == 0:
                time.sleep(0.5)
        raise FetchError(f"雪球行业获取失败: {code} ({last})")


def parse_kline_payload(payload: Any, code: str) -> list[list[Any]]:
    data = (payload or {}).get("data") or {}
    item = data.get(code) or {}
    bars = item.get("qfqday") or item.get("day") or []
    return [list(bar) for bar in bars if isinstance(bar, (list, tuple)) and len(bar) >= 6]


def parallel_map(fn: Callable[[Any], Any], items: Iterable[Any], workers: int = WORKERS) -> list[Any]:
    """按输入顺序返回结果；单点异常作为结果值返回，由调用方降级。"""
    items = list(items)
    output: list[Any] = [None] * len(items)
    if not items:
        return output
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fn, item): index for index, item in enumerate(items)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                output[index] = future.result()
            except Exception as exc:  # noqa: BLE001 - 单点失败降级
                output[index] = exc
    return output


# ── 抓取阶段 ─────────────────────────────────────────────────


def fetch_benchmark_bars(fetcher: Any) -> list[list[Any]]:
    payload = fetcher.get_json(kline_url(BENCHMARK_CODE, 20))
    bars = parse_kline_payload(payload, BENCHMARK_CODE)
    if not bars:
        raise FetchError("上证指数无日K数据")
    return bars


def fetch_catalog(fetcher: Any, board_type: str) -> list[dict[str, Any]]:
    payload = fetcher.get_json(rank_url(board_type))
    rows = ((payload or {}).get("data") or {}).get("rank_list") or []
    catalog = []
    for row in rows:
        code, name = row.get("code"), row.get("name")
        if code and name:
            catalog.append({"code": str(code), "name": str(name)})
    if not catalog:
        raise FetchError(f"行业目录为空: {board_type}")
    return catalog


def fetch_sector_records(
    fetcher: Any,
    catalog: list[dict[str, Any]],
    window: Window,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    def fetch_one(item: dict[str, Any]) -> dict[str, Any]:
        payload = fetcher.get_json(kline_url(item["code"], 20))
        return {"code": item["code"], "name": item["name"], "bars": parse_kline_payload(payload, item["code"])}

    raw = parallel_map(fetch_one, catalog)
    records: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    for index, item in enumerate(catalog):
        code, name = item["code"], item["name"]
        fetched = raw[index]
        if isinstance(fetched, Exception) or not isinstance(fetched, dict) or not fetched.get("bars"):
            records.append({"code": code, "name": name, "valid": False})
            excluded.append({"code": code, "name": name, "reason": "K线缺失"})
            continue
        bars = fetched["bars"]
        if not series_matches_window(bars, window):
            records.append({"code": code, "name": name, "valid": False})
            excluded.append({"code": code, "name": name, "reason": "窗口不匹配"})
            continue
        closes = closes_for_window(bars, window)
        r3 = pct_change(closes[window.d0], closes[window.d3]) if closes else None
        if r3 is None:
            records.append({"code": code, "name": name, "valid": False})
            excluded.append({"code": code, "name": name, "reason": "价格异常"})
            continue
        amount_avg, _ = avg_amount_wan(code, bars, window)
        records.append(
            {
                "code": code,
                "name": name,
                "valid": True,
                "r3": round(r3, 6),
                "excess": None,  # 基准 r3 在 collect 中统一补
                "amount_avg_wan": round(amount_avg, 2) if amount_avg is not None else None,
            }
        )
    return records, excluded


def fetch_board_constituents(fetcher: Any, code: str) -> tuple[list[dict[str, str]], int | None]:
    members: list[dict[str, str]] = []
    seen: set[str] = set()
    offset = 0
    total: int | None = None
    while True:
        payload = fetcher.get_json(board_url(code, offset))
        data = (payload or {}).get("data") or {}
        rows = data.get("rank_list") or []
        if total is None:
            total = int(data.get("total") or 0)
        for row in rows:
            row_code = str(row.get("code") or "")
            row_name = str(row.get("name") or "")
            if row_code and row_code not in seen:
                seen.add(row_code)
                members.append({"code": row_code, "name": row_name})
        offset += 200
        if not rows or (total is not None and offset >= total):
            break
    return members, total


def fetch_kcb_symbols(fetcher: Any) -> list[dict[str, str]]:
    symbols: list[dict[str, str]] = []
    seen: set[str] = set()
    for page in range(1, 8):
        payload = fetcher.get_json(kcb_page_url(page))
        if not isinstance(payload, list):
            raise FetchError(f"科创板列表第 {page} 页格式异常")
        if not payload:
            break
        for row in payload:
            code = str(row.get("symbol") or "")
            name = str(row.get("name") or "")
            if code and code not in seen:
                seen.add(code)
                symbols.append({"code": code, "name": name})
    return symbols


def get_kcb_industry(
    fetcher: Any,
    symbols: list[dict[str, str]],
    now: datetime,
    cache_path: Path,
    overrides: dict[str, str],
    catalog_names: set[str],
    write_cache: bool,
) -> dict[str, Any]:
    cache = load_json_file(cache_path, None)
    cached_map = cache.get("industries") if isinstance(cache, dict) else None
    if not isinstance(cached_map, dict):
        cached_map = {}
    cached_map = {str(key): str(value) for key, value in cached_map.items()}

    if kcb_cache_is_fresh(cache, now):
        return {
            "map": cached_map,
            "source": "cache",
            "cache_fresh": True,
            "failed_count": 0,
            "unmapped_count": sum(
                1 for item in symbols if align_kcb_industry(item["code"], cached_map.get(item["code"]), catalog_names, overrides) is None
            ),
        }

    results = parallel_map(fetcher.xueqiu_industry, [item["code"] for item in symbols], WORKERS)
    fresh_map: dict[str, str] = {}
    failed = 0
    for index, item in enumerate(symbols):
        code = item["code"]
        result = results[index]
        if isinstance(result, Exception):
            failed += 1
            continue
        if result:
            fresh_map[code] = result

    source = "xueqiu"
    cache_written = False
    merged = dict(cached_map)
    merged.update(fresh_map)
    if not fresh_map:
        source = "cache_stale" if cached_map else "none"
    elif failed and len(fresh_map) < len(symbols) * 0.9:
        # 大面积失败不落缓存，避免把“缺失”固化 7 天；本次仍用已拿到的数据。
        source = "xueqiu_partial"
    elif write_cache:
        try:
            write_json_atomic(
                cache_path,
                {"fetched_at": now.replace(microsecond=0).isoformat(), "source": "xueqiu", "industries": merged},
            )
            cache_written = True
        except Exception:  # noqa: BLE001 - 缓存非关键，失败不影响本次结果
            cache_written = False

    unmapped = 0
    for item in symbols:
        aligned = align_kcb_industry(item["code"], merged.get(item["code"]), catalog_names, overrides)
        if aligned is None:
            unmapped += 1
    return {
        "map": merged,
        "source": source,
        "cache_fresh": False,
        "cache_written": cache_written,
        "failed_count": failed,
        "unmapped_count": unmapped,
    }


# ── 产物构建 ─────────────────────────────────────────────────


def round_numbers(value: Any, digits: int = 6) -> Any:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON 不允许 NaN/Infinity")
        return round(value, digits)
    if isinstance(value, dict):
        return {key: round_numbers(item, digits) for key, item in value.items()}
    if isinstance(value, list):
        return [round_numbers(item, digits) for item in value]
    return value


def write_json_atomic(path: Path, value: Any) -> None:
    last: Exception | None = None
    for attempt in range(2):
        try:
            dump_json_utf8(path, round_numbers(value))
            return
        except Exception as exc:  # noqa: BLE001 - 文件占用等写失败重试一次
            last = exc
            if attempt == 0:
                time.sleep(0.5)
    raise FatalError(f"写入失败（文件可能被占用）: {path} ({last})")


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    last: Exception | None = None
    for attempt in range(2):
        handle = None
        tmp_name = None
        try:
            handle, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                handle = None
                stream.write(text)
            os.replace(tmp_name, path)
            return
        except OSError as exc:
            last = exc
            if handle is not None:
                try:
                    os.close(handle)
                except OSError:
                    pass
            if tmp_name:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
            if attempt == 0:
                time.sleep(0.5)
    raise FatalError(f"写入失败（文件可能被占用）: {path} ({last})")


def fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:+.2f}%"


def fmt_pp(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:+.2f}pp"


def fmt_amount(value: float | None) -> str:
    return "—" if value is None else f"{value:,.0f}"


def data_status_tokens(detail: dict[str, Any]) -> str:
    tokens: list[str] = []
    breadth = detail["breadth"]
    if breadth["weight_driven"]:
        tokens.append("权重拉动")
    if breadth["narrow_breadth"]:
        tokens.append("广度不足")
    if breadth["no_strong"]:
        tokens.append("无强势股")
    if detail["constituent_status"]["incomplete"]:
        tokens.append("成分股不全")
    if detail["excluded_counts"].get("kline_missing"):
        tokens.append("K线缺失")
    return "正常" if not tokens else " · ".join(tokens)


def render_markdown(report: dict[str, Any]) -> str:
    window = report["window"]
    lines: list[str] = []
    lines.append(f"# 市场热点 {report['report_date']}")
    lines.append("")
    lines.append(
        f"> 数据窗口：{window['trading_days'][0]} ~ {window['end_date']}（3 个交易日，基准日 {window['base_date']}）"
        f"｜基准：上证指数 {fmt_pct(window['benchmark_r3'])}｜生成：{report['generated_at']}"
    )
    lines.append("")
    lines.append("## 结论速览")
    lines.append("")
    top_sw2 = report["top_industries"]
    if top_sw2:
        lead = top_sw2[0]
        lead_stock = lead["stocks"][0]["name"] if lead["stocks"] else "—"
        lines.append(
            f"- 申万二级领涨：{lead['name']}（{fmt_pct(lead['r3'])}，超额 {fmt_pp(lead['excess'])}），"
            f"强势/合格 {lead['breadth']['strong_count']}/{lead['counts']['data_eligible']}；领涨股 {lead_stock}。"
        )
    else:
        lines.append("- 申万二级：无有效行业数据。")
    top_sw1 = report["top_sw1"]
    if top_sw1:
        lines.append(f"- 申万一级领涨：{top_sw1[0]['name']}（{fmt_pct(top_sw1[0]['r3'])}）。")
    sw2_rank = report["industry_rankings"]["sw2"]
    sw1_rank = report["industry_rankings"]["sw1"]
    lines.append(
        f"- 数据完整性：有效二级 {sw2_rank['valid_count']}/{sw2_rank['expected_count']}，"
        f"有效一级 {sw1_rank['valid_count']}/{sw1_rank['expected_count']}。"
    )
    lines.append("")
    lines.append("## 口径")
    lines.append("")
    lines.append(
        "- 近 3 个完整交易日（D1~D3），涨跌幅 = D3 收盘 / D0 收盘 - 1；"
        "基准 = 上证指数，超额 = 行业 r3 - 基准 r3。"
    )
    lines.append(
        "- 行业：申万二级（124 个，主表）与申万一级（31 个，对照），按 r3 降序；"
        "并列按板块近 3 日日均成交额降序、代码升序。"
    )
    lines.append(
        "- 股票范围：沪深 A（含主板/创业板/科创板），剔除 ST/退市/停牌/上市不足 60 根有效 bar；"
        "近 3 日（D1~D3）日均成交额 < 5000 万元剔除；剔北交所与 B 股。"
    )
    if not window["include_today"]:
        lines.append("- 运行时间早于 15:05，已剔除当日盘中 bar。")
    lines.append("")
    lines.append("## 申万二级 Top 5（主表）")
    lines.append("")
    lines.append("| 排名 | 行业 | 3日涨幅 | 超额 | 强势/合格 | 中位数 | 领涨股 | 数据 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for index, item in enumerate(top_sw2, 1):
        lead = item["stocks"][0]["name"] if item["stocks"] else "—"
        median = item["breadth"]["median"]
        lines.append(
            f"| {index} | {item['name']} | {fmt_pct(item['r3'])} | {fmt_pp(item['excess'])} | "
            f"{item['breadth']['strong_count']}/{item['counts']['data_eligible']} | "
            f"{fmt_pct(median) if median is not None else '—'} | {lead} | {data_status_tokens(item)} |"
        )
    if not top_sw2:
        lines.append("| — | 无有效行业 | — | — | — | — | — | — |")
    lines.append("")
    lines.append("## 原因分析（申万二级 Top 5）")
    lines.append("")
    for index, item in enumerate(top_sw2, 1):
        lines.append(f"### {index}. {item['name']}（{fmt_pct(item['r3'])}，超额 {fmt_pp(item['excess'])}）")
        lines.append("")
        lead = item["stocks"][0] if item["stocks"] else None
        lead_text = f"领涨 {lead['name']}（{fmt_pct(lead['r3'])}）" if lead else "无领涨股"
        top_names = "、".join(f"{stock['name']}（{fmt_pct(stock['r3'])}）" for stock in item["stocks"][:5]) or "无"
        lines.append(
            f"- 盘面：{item['breadth']['strong_count']}/{item['counts']['data_eligible']} 只强势"
            f"（前 5：{top_names}），"
            f"中位数 {fmt_pct(item['breadth']['median']) if item['breadth']['median'] is not None else '—'}，{lead_text}。"
        )
        lines.append("- 原因：<!-- AI 填写 -->")
        lines.append("- 依据：<!-- AI 填写 -->")
        lines.append("")
    lines.append("## 申万一级 Top 5（对照）")
    lines.append("")
    lines.append("| 排名 | 行业 | 3日涨幅 | 超额 |")
    lines.append("| --- | --- | --- | --- |")
    for index, item in enumerate(report["top_sw1"], 1):
        lines.append(f"| {index} | {item['name']} | {fmt_pct(item['r3'])} | {fmt_pp(item['excess'])} |")
    if not report["top_sw1"]:
        lines.append("| — | 一级数据不可用 | — | — |")
    lines.append("")
    lines.append("## 全量排名")
    lines.append("")
    for label, key in (("申万二级", "sw2"), ("申万一级", "sw1")):
        ranking = report["industry_rankings"][key]
        lines.append("<details>")
        lines.append(f"<summary>{label}（{ranking['valid_count']} 个有效 / 目录 {ranking['expected_count']} 个）</summary>")
        lines.append("")
        lines.append("| 排名 | 行业 | 3日涨幅 | 超额 | 日均成交额（万元） |")
        lines.append("| --- | --- | --- | --- | --- |")
        for index, item in enumerate(ranking["ranking"], 1):
            lines.append(
                f"| {index} | {item['name']} | {fmt_pct(item['r3'])} | {fmt_pp(item['excess'])} | "
                f"{fmt_amount(item['amount_avg_wan'])} |"
            )
        if ranking["excluded"]:
            lines.append("")
            lines.append("剔除：" + "；".join(f"{item['name']}（{item['reason']}）" for item in ranking["excluded"]))
        lines.append("")
        lines.append("</details>")
        lines.append("")
    lines.append("## 数据质量")
    lines.append("")
    issues = report["data_quality"]["issues"]
    if issues:
        for issue in issues:
            lines.append(f"- {issue}")
    else:
        lines.append("- 无异常。")
    lines.append(
        f"- 股票池：raw {report['universe']['raw']} / 有效 A 股 {report['universe']['universe_eligible']} / "
        f"合格 {report['universe']['data_eligible']} / 强势 {report['universe']['strong']}。"
    )
    lines.append("")
    return "\n".join(lines)


def build_top_industry(
    rank: int,
    sector: dict[str, Any],
    member_map: dict[str, str],
    stock_results: dict[str, dict[str, Any]],
    bench_r3: float,
    board_total: int | None,
    kcb_member_count: int,
    board_failed: bool,
    kcb_ok: bool,
) -> dict[str, Any]:
    results = [stock_results[code] for code in member_map if code in stock_results]
    summary = summarize_stock_results(results, bench_r3)
    eligible_r3 = [item["r3"] for item in summary["eligible"]]
    breadth = compute_breadth(eligible_r3, sector["r3"], strong_count=len(summary["strong"]))

    stocks = [
        {
            "code": item["code"],
            "name": item["name"],
            "r3": item["r3"],
            "excess": item["excess"],
            "amount_avg_wan": item["amount_avg_wan"],
            "absolute_up": item["absolute_up"],
            "amount_source": item["amount_source"],
        }
        for item in summary["strong"]
    ]
    detail = {
        "rank": rank,
        "code": sector["code"],
        "name": sector["name"],
        "r3": sector["r3"],
        "excess": sector["excess"],
        "amount_avg_wan": sector["amount_avg_wan"],
        "breadth": breadth,
        "counts": summary["counts"],
        "stocks": stocks,
        "excluded_counts": summary["excluded_counts"],
        "excluded_examples": summary["excluded_examples"],
        "constituent_status": {
            "rank_list_total": board_total,
            "rank_fetched": len(member_map) - kcb_member_count,
            "kcb_members": kcb_member_count,
            "unique": len(member_map),
            "incomplete": bool(board_failed or not kcb_ok),
        },
    }
    return detail


# ── 主流程 ───────────────────────────────────────────────────


def collect(
    fetcher: Any,
    now: datetime,
    reports_dir: Path,
    write_cache: bool = True,
) -> dict[str, Any]:
    issues: list[str] = []
    benchmark_bars = fetch_benchmark_bars(fetcher)
    window = compute_window(benchmark_bars, now)
    closes = closes_for_window(benchmark_bars, window)
    if closes is None:
        raise FatalError("基准指数窗口数据缺失")
    bench_r3 = pct_change(closes[window.d0], closes[window.d3])
    if bench_r3 is None:
        raise FatalError("基准指数涨幅计算失败")

    sw2_catalog = fetch_catalog(fetcher, "hy2")
    sw2_names = {item["name"] for item in sw2_catalog}
    sw2_records, sw2_excluded = fetch_sector_records(fetcher, sw2_catalog, window)
    for record in sw2_records:
        if record.get("valid"):
            record["excess"] = round(record["r3"] - bench_r3, 6)
    sw2_valid = rank_sector_records(sw2_records)
    if not sw2_valid:
        raise FatalError("无有效申万二级行业数据")

    try:
        sw1_catalog = fetch_catalog(fetcher, "hy")
        sw1_records, sw1_excluded = fetch_sector_records(fetcher, sw1_catalog, window)
        for record in sw1_records:
            if record.get("valid"):
                record["excess"] = round(record["r3"] - bench_r3, 6)
        sw1_valid = rank_sector_records(sw1_records)
    except Exception as exc:  # noqa: BLE001 - 一级失败仅降级
        sw1_catalog = []
        sw1_valid = []
        sw1_excluded = []
        issues.append(f"申万一级目录/行情获取失败，仅输出二级：{exc}")

    top_sw2 = sw2_valid[:5]
    if len(top_sw2) < 5:
        issues.append(f"有效申万二级行业仅 {len(top_sw2)} 个，Top 5 不完整。")

    # 成分股：仅抓 Top 5 二级行业（满足强势股/广度口径，避免全市场 5000 只 K 线的无谓开销）。
    overrides = load_overrides(OVERRIDES_PATH)
    kcb_symbols: list[dict[str, str]] = []
    kcb_ok = True
    try:
        kcb_symbols = fetch_kcb_symbols(fetcher)
    except Exception as exc:  # noqa: BLE001 - 科创板失败降级
        kcb_ok = False
        issues.append(f"科创板代码列表获取失败，科创板成分未纳入：{exc}")
    kcb_cache_path = reports_dir / CACHE_RELATIVE
    if kcb_symbols:
        kcb_info = get_kcb_industry(
            fetcher,
            kcb_symbols,
            now,
            kcb_cache_path,
            overrides,
            sw2_names,
            write_cache,
        )
    else:
        kcb_info = {
            "map": {},
            "source": "none",
            "cache_fresh": False,
            "failed_count": 0,
            "unmapped_count": 0,
        }
    if kcb_info["source"] == "xueqiu" and write_cache and not kcb_info.get("cache_written"):
        issues.append("科创板行业缓存写入失败，本次结果不受影响。")
    if kcb_info["source"] == "cache_stale":
        issues.append("雪球行业接口失败，使用过期缓存并在报告注明。")
    elif kcb_info["source"] == "none" and kcb_symbols:
        issues.append("雪球行业接口失败且无缓存，科创板股票按未归类处理。")
    elif kcb_info["source"] == "xueqiu_partial":
        issues.append(f"雪球行业接口部分失败（{kcb_info['failed_count']} 只），缺失部分沿用旧缓存/未归类。")
    if kcb_info["unmapped_count"]:
        issues.append(f"科创板 {kcb_info['unmapped_count']} 只行业名称无法对齐申万二级，按未归类处理。")
    if kcb_symbols and len(kcb_symbols) < 600:
        issues.append(f"科创板代码列表仅 {len(kcb_symbols)} 只，可能不完整。")

    kcb_by_industry: dict[str, list[dict[str, str]]] = {}
    kcb_names = {item["code"]: item["name"] for item in kcb_symbols}
    for item in kcb_symbols:
        aligned = align_kcb_industry(item["code"], kcb_info["map"].get(item["code"]), sw2_names, overrides)
        if aligned:
            kcb_by_industry.setdefault(aligned, []).append(item)

    board_total: dict[str, int | None] = {}
    board_failed: dict[str, bool] = {}
    members_by_sector: dict[str, dict[str, str]] = {}
    for sector in top_sw2:
        try:
            members, total = fetch_board_constituents(fetcher, sector["code"])
            members_by_sector[sector["code"]] = {item["code"]: item["name"] for item in members}
            board_total[sector["code"]] = total
            board_failed[sector["code"]] = False
        except Exception as exc:  # noqa: BLE001 - 单板块失败降级
            members_by_sector[sector["code"]] = {}
            board_total[sector["code"]] = None
            board_failed[sector["code"]] = True
            issues.append(f"行业 {sector['name']} 成分股获取失败：{exc}")

    # 合并科创板成分
    merged_members: dict[str, dict[str, str]] = {}
    kcb_member_counts: dict[str, int] = {}
    for sector in top_sw2:
        merged = dict(members_by_sector[sector["code"]])
        kcb_members = kcb_by_industry.get(sector["name"], [])
        kcb_member_counts[sector["code"]] = len(kcb_members)
        for item in kcb_members:
            merged.setdefault(item["code"], item["name"])
        merged_members[sector["code"]] = merged

    all_codes = sorted({code for members in merged_members.values() for code in members})
    stock_bars_raw = parallel_map(
        lambda code: parse_kline_payload(fetcher.get_json(kline_url(code, 70)), code),
        all_codes,
        WORKERS,
    )
    stock_results: dict[str, dict[str, Any]] = {}
    stock_kline_failures = 0
    names = {code: name for members in merged_members.values() for code, name in members.items()}
    for index, code in enumerate(all_codes):
        bars = stock_bars_raw[index]
        if isinstance(bars, Exception) or not bars:
            stock_kline_failures += 1
            stock_results[code] = {
                "code": code,
                "name": names.get(code, ""),
                "excluded_reason": "kline_missing",
            }
            continue
        stock_results[code] = classify_stock(code, names.get(code, ""), bars, window, bench_r3)
    if stock_kline_failures:
        issues.append(f"{stock_kline_failures} 只股票日 K 获取失败，已剔除并计入 K线缺失。")

    top_industries: list[dict[str, Any]] = []
    for index, sector in enumerate(top_sw2, 1):
        top_industries.append(
            build_top_industry(
                index,
                sector,
                merged_members[sector["code"]],
                stock_results,
                bench_r3,
                board_total[sector["code"]],
                kcb_member_counts[sector["code"]],
                board_failed[sector["code"]],
                kcb_ok,
            )
        )

    # 全局股票池计数（Top 5 二级成分合并；申万行业互斥，故无重复）
    all_results = list(stock_results.values())
    universe_summary = summarize_stock_results(all_results, bench_r3)
    universe = {
        "scope": "top5_sw2_constituents",
        "raw": universe_summary["counts"]["raw"],
        "universe_eligible": universe_summary["counts"]["universe_eligible"],
        "data_eligible": universe_summary["counts"]["data_eligible"],
        "strong": universe_summary["counts"]["strong"],
        "below_benchmark": universe_summary["counts"]["below_benchmark"],
        "amount_threshold_wan": AMOUNT_THRESHOLD_WAN,
        "excluded_counts": universe_summary["excluded_counts"],
    }

    def ranking_block(catalog: list[dict[str, Any]], valid: list[dict[str, Any]], excluded: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "expected_count": len(catalog),
            "valid_count": len(valid),
            "ranking": [
                {
                    "rank": index,
                    "code": item["code"],
                    "name": item["name"],
                    "r3": item["r3"],
                    "excess": round(item["r3"] - bench_r3, 6),
                    "amount_avg_wan": item.get("amount_avg_wan"),
                }
                for index, item in enumerate(valid, 1)
            ],
            "excluded": excluded,
        }

    amount_estimated = sum(
        1 for item in all_results if not item.get("excluded_reason") and item.get("amount_source") == "estimated"
    )
    if amount_estimated:
        issues.append(f"{amount_estimated} 只股票成交额字段缺失，按 volume×close 估算（amount_source=estimated）。")

    data_quality = {
        "window_note": f"{window.d1} ~ {window.d3}，基准日 {window.d0}，含当日={window.include_today}",
        "issues": issues,
        "stock_kline_failures": stock_kline_failures,
        "amount_estimated_stocks": amount_estimated,
        "valid_sw2": len(sw2_valid),
        "valid_sw1": len(sw1_valid),
        "kcb_source": kcb_info["source"],
        "kcb_unmapped": kcb_info["unmapped_count"],
    }

    report = {
        "schema_version": 1,
        "generated_at": now.replace(microsecond=0).isoformat(),
        "report_date": now.date().isoformat(),
        "window": {
            "base_date": window.d0,
            "trading_days": window.trading_days,
            "end_date": window.d3,
            "include_today": window.include_today,
            "benchmark": BENCHMARK_CODE,
            "benchmark_r3": round(bench_r3, 6),
        },
        "universe": universe,
        "industry_rankings": {
            "sw2": ranking_block(sw2_catalog, sw2_valid, sw2_excluded),
            "sw1": ranking_block(sw1_catalog, sw1_valid, sw1_excluded),
        },
        "top_industries": top_industries,
        "top_sw1": [
            {
                "rank": index,
                "code": item["code"],
                "name": item["name"],
                "r3": item["r3"],
                "excess": round(item["r3"] - bench_r3, 6),
                "amount_avg_wan": item.get("amount_avg_wan"),
            }
            for index, item in enumerate(sw1_valid[:5], 1)
        ],
        "kcb": {
            "total": len(kcb_symbols),
            "industry_source": kcb_info["source"],
            "unmapped_count": kcb_info["unmapped_count"],
            "cache_path": str(kcb_cache_path),
            "cache_fresh": kcb_info["cache_fresh"],
            "override_count": len(overrides),
        },
        "data_quality": data_quality,
        "sources": {
            "benchmark": "腾讯 newfqkline",
            "sector_index": "腾讯 newfqkline",
            "industry_catalog": "腾讯 getRank",
            "constituents": "腾讯 getBoardRankList",
            "kcb_codes": "新浪 Market_Center",
            "kcb_industry": "雪球 f10 industry",
            "east" + "money_used": False,
        },
    }
    report["markdown"] = render_markdown(report)
    return report


def verify_counts_identity(report: dict[str, Any]) -> None:
    universe = report["universe"]
    excluded = universe["excluded_counts"]
    pre = excluded.get("non_a_share", 0) + excluded.get("delisted", 0) + excluded.get("st", 0)
    if universe["raw"] != universe["universe_eligible"] + pre:
        raise AssertionError("universe 计数恒等式失败（raw）")
    if universe["universe_eligible"] - universe["data_eligible"] < 0:
        raise AssertionError("universe 计数恒等式失败（post）")
    if universe["data_eligible"] != universe["strong"] + universe["below_benchmark"]:
        raise AssertionError("universe 计数恒等式失败（strong/below）")
    for detail in report["top_industries"]:
        counts = detail["counts"]
        item_pre = (
            detail["excluded_counts"].get("non_a_share", 0)
            + detail["excluded_counts"].get("delisted", 0)
            + detail["excluded_counts"].get("st", 0)
        )
        if counts["raw"] != counts["universe_eligible"] + item_pre:
            raise AssertionError(f"{detail['name']} 计数恒等式失败（raw）")
        if counts["data_eligible"] != counts["strong"] + counts["below_benchmark"]:
            raise AssertionError(f"{detail['name']} 计数恒等式失败（strong/below）")


# ── CLI ──────────────────────────────────────────────────────


def print_summary(report: dict[str, Any]) -> None:
    window = report["window"]
    print(f"窗口：{window['trading_days'][0]} ~ {window['end_date']}（基准日 {window['base_date']}，含当日={window['include_today']}）")
    print(f"基准：上证指数 {fmt_pct(window['benchmark_r3'])}")
    print("申万二级 Top 5：")
    for item in report["top_industries"]:
        print(
            f"  {item['rank']}. {item['name']} {fmt_pct(item['r3'])} 超额 {fmt_pp(item['excess'])} "
            f"强势/合格 {item['breadth']['strong_count']}/{item['counts']['data_eligible']} "
            f"强势股={','.join(stock['name'] for stock in item['stocks'][:5]) or '无'}"
        )
    print("申万一级 Top 5：" + "、".join(f"{item['name']} {fmt_pct(item['r3'])}" for item in report["top_sw1"]))
    print(
        f"股票池：raw {report['universe']['raw']} 合格 {report['universe']['data_eligible']} "
        f"强势 {report['universe']['strong']}"
    )


def selftest() -> int:
    def bar(day: str, close: float, amount: float = 10000.0, volume: float = 10000.0, precise: bool = True) -> list[Any]:
        item: list[Any] = [day, str(close), str(close), str(close), str(close), str(volume), {}]
        if precise:
            item.extend([str(1.0), str(amount), ""])
        return item

    try:
        # 窗口切换 15:04:59 / 15:05:00
        days = ["2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09"]
        bench = [bar(day, 100 + index) for index, day in enumerate(days)]
        early = compute_window(bench, datetime(2026, 11, 9, 15, 4, 59))
        assert (early.d0, early.d1, early.d2, early.d3, early.include_today) == (
            "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", False
        )
        late = compute_window(bench, datetime(2026, 11, 9, 15, 5, 0))
        assert (late.d1, late.d2, late.d3, late.include_today) == ("2026-11-05", "2026-11-06", "2026-11-09", True)

        # r3
        assert abs(pct_change(100.0, 110.0) - 0.1) < 1e-12
        assert pct_change(0.0, 110.0) is None

        # 并列排序：r3 同 → 成交额降序 → code 升序
        records = [
            {"code": "pt2", "name": "B", "valid": True, "r3": 0.1, "amount_avg_wan": 100.0},
            {"code": "pt1", "name": "A", "valid": True, "r3": 0.1, "amount_avg_wan": 200.0},
            {"code": "pt3", "name": "C", "valid": True, "r3": 0.1, "amount_avg_wan": 200.0},
        ]
        ranked = rank_sector_records(records)
        assert [item["code"] for item in ranked] == ["pt1", "pt3", "pt2"]

        # Top5
        assert len(top_sectors([{**item, "code": f"pt{i}"} for i, item in enumerate(records * 3)], 5)) == 5

        # 广度边界
        breadth = compute_breadth([0.01, 0.02, 0.03], 0.04)
        assert breadth["narrow_breadth"] is False and breadth["weight_driven"] is True
        breadth = compute_breadth([0.05, 0.06, -0.01], 0.01)
        assert breadth["breadth_ratio"] == round(2 / 3, 6)
        assert compute_breadth([], 0.01)["no_strong"] is True

        # ST / 退市 / B 股 / 北交所
        w = Window("2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", False)
        window_days = [date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5), date(2026, 11, 6)]
        prior_days = [date(2026, 8, 1) + timedelta(days=index) for index in range(56)]
        all_days = prior_days + window_days  # 恰好 60 根
        stock_bars = [bar(day.isoformat(), 10.0) for day in all_days]
        assert classify_stock("sh600000", "ST测试", stock_bars, w, 0.0)["excluded_reason"] == "st"
        assert classify_stock("sh600000", "退市测试", stock_bars, w, 0.0)["excluded_reason"] == "delisted"
        assert classify_stock("sh900001", "B股", stock_bars, w, 0.0)["excluded_reason"] == "non_a_share"
        assert classify_stock("bj430047", "北交所", stock_bars, w, 0.0)["excluded_reason"] == "non_a_share"

        # 59 / 60 根
        short_days = prior_days + window_days[:3]
        short_bars = [bar(day.isoformat(), 10.0) for day in short_days]
        assert len(short_bars) == 59
        assert classify_stock("sh600000", "测试", short_bars, w, 0.0)["excluded_reason"] == "listed_short"
        assert classify_stock("sh600000", "测试", stock_bars, w, 0.0)["excluded_reason"] is None

        # 停牌：最后 bar 不等于 D3 / D3 成交额为 0
        halted_days = prior_days + [date(2026, 8, 1) + timedelta(days=56)] + window_days[:3]
        assert len(halted_days) == 60
        halted = [bar(day.isoformat(), 10.0) for day in halted_days]
        assert classify_stock("sh600000", "测试", halted, w, 0.0)["excluded_reason"] == "suspended"
        zero_amount = [bar(day.isoformat(), 10.0) for day in all_days]
        zero_amount[-1] = bar("2026-11-06", 10.0, amount=0.0)
        assert classify_stock("sh600000", "测试", zero_amount, w, 0.0)["excluded_reason"] == "suspended"

        # 成交额门槛 4999.99 / 5000
        low = [bar(day.isoformat(), 10.0, amount=4999.99) for day in all_days]
        ok = [bar(day.isoformat(), 10.0, amount=5000.0) for day in all_days]
        assert classify_stock("sh600000", "测试", low, w, 0.0)["excluded_reason"] == "low_amount"
        assert classify_stock("sh600000", "测试", ok, w, 0.0)["excluded_reason"] is None

        # 688 估算单位
        estimate_bar = [bar("2026-11-06", 10.0, precise=False, volume=10000.0)]
        assert estimate_amount_wan("sh688001", estimate_bar[0]) == 10.0
        assert estimate_amount_wan("sh600000", estimate_bar[0]) == 1000.0

        # 科创板名称对齐 / 歧义 / override
        names = {"半导体", "IT服务Ⅱ", "专用设备", "塑料"}
        assert align_kcb_industry("sh688001", "IT服务", names, {}) == "IT服务Ⅱ"
        assert align_kcb_industry("sh688001", "军工装备", names, {}) is None
        assert align_kcb_industry("sh688001", "塑料制品", names, {}) == "塑料"
        assert align_kcb_industry("sh688001", "军工装备", names, {"sh688001": "专用设备"}) == "专用设备"

        # 缓存 TTL
        now = datetime(2026, 11, 9, 12, 0, 0)
        assert kcb_cache_is_fresh({"fetched_at": "2026-11-03T12:00:00"}, now) is True
        assert kcb_cache_is_fresh({"fetched_at": "2026-11-02T11:59:59"}, now) is False
        assert kcb_cache_is_fresh(None, now) is False

        # 渲染占位
        report = {
            "report_date": "2026-11-09",
            "generated_at": "2026-11-09T12:00:00",
            "window": {
                "base_date": w.d0,
                "trading_days": w.trading_days,
                "end_date": w.d3,
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
                    "counts": {
                        "raw": 5,
                        "universe_eligible": 5,
                        "data_eligible": 5,
                        "strong": 2,
                        "below_benchmark": 3,
                    },
                    "stocks": [{"name": "甲", "r3": 0.08}, {"name": "乙", "r3": 0.06}],
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
            "universe": {"raw": 0, "universe_eligible": 0, "data_eligible": 0, "strong": 0},
        }
        markdown = render_markdown(report)
        for marker in ("# 市场热点 2026-11-09", "## 申万二级 Top 5（主表）", "## 原因分析（申万二级 Top 5）", "## 数据质量"):
            assert marker in markdown
        assert markdown.count("<!-- AI 填写 -->") == 2

        print("SELFTEST PASS")
        return 0
    except AssertionError as exc:
        import traceback

        traceback.print_exc()
        print(f"SELFTEST FAIL: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"SELFTEST FAIL: {exc}")
        return 1


def main(argv: list[str] | None = None, now: datetime | None = None, fetcher: Any | None = None, reports_dir: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description="市场热点分析：近 3 日行业涨幅与强势股底稿")
    parser.add_argument("--dry-run", action="store_true", help="抓取并打印摘要，不写任何文件")
    parser.add_argument("--selftest", action="store_true", help="纯函数自检，不触网不落盘")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    moment = now or datetime.now()
    target_dir = reports_dir or REPORTS_DIR
    fetcher = fetcher or HttpFetcher()
    try:
        report = collect(fetcher, moment, target_dir, write_cache=not args.dry_run)
        verify_counts_identity(report)
    except FatalError as exc:
        print(f"[致命] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - 致命抓取/计算失败
        print(f"[致命] {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print_summary(report)
        print("dry-run：未写入任何文件。")
        return 0

    markdown_path = target_dir / f"市场热点_{report['report_date']}.md"
    json_path = target_dir / f"市场热点底稿_{report['report_date']}.json"
    payload = {key: value for key, value in report.items() if key != "markdown"}
    try:
        # 先写底稿再写报告：AI 阶段以 JSON 为准，避免出现新 MD 配旧 JSON。
        write_json_atomic(json_path, payload)
        write_text_atomic(markdown_path, report["markdown"])
    except FatalError as exc:
        print(f"[致命] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - 写盘异常统一退出 2
        print(f"[致命] 写入失败: {exc}", file=sys.stderr)
        return 2
    print_summary(report)
    print(f"已写入：{markdown_path}")
    print(f"已写入：{json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
