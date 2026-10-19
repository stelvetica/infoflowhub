"""交易日门禁：判定「今天这个市场是否开市」，供调度循环决定要不要干活。

为什么需要：调度原来只看钟点——周末与节假日到点照样抓一遍订阅、跑一遍 skill，白烧一次
浏览器与模型调用，产物还全是隔夜旧闻。把「今天是交易日吗」抽成一处判定，调度侧只消费
结论（放行 / 不放行），不自己拼证据。

证据链（按可靠性从低到高排查，首个决定性结论即返回）：
a) 周末 → 闭市（离线、零成本，覆盖约 2/7 的天数）；
b) 蓝宝书「晨会版 / 午间版」当日已刊 → 开市（离线证据，见 bluebook_published_today）；
c) 东财日 K 序列含今日 → 开市；序列非空且最新日期早于今日 → 闭市（需网络，仅开盘后采信）；
d) 以上都取不到 → unknown，保守放行。

放行口径只有一条：verdict != "closed"（unknown 也放行）。门禁的价值在于「确有证据证明闭市
时省一次流程」，而不是「证据不足时拦住流程」——证据不足时拦住会把网络抖动变成整天不跑。

不承担：盘中午休、半日市、港美股各自的节假日差异取证（港美股当前复用同一条链，仅 secid 不同）。
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Iterable
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parents[1]

# 各市场只差一个 secid，判定链共用——加市场只加一行，不动逻辑。
MARKETS = {
    "A": {"name": "A股", "secid": "1.000001"},
    "H": {"name": "港股", "secid": "100.HSI"},
    "US": {"name": "美股", "secid": "100.SPX"},
}
DEFAULT_MARKET = "A"
KLINE_API = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
KLINE_TIMEOUT_SECONDS = 5
# 盘前日 K 里没有今日这一行（当天还没收盘，序列尚未追加），故「今日缺席」这个信号
# 只有开盘之后才可采信；盘前采信会天天误判闭市。
SESSION_OPEN_PROBE = time(9, 30)
BLUEBOOK_DIRS = (BASE_DIR / "data" / "alphapai_markdown",)

VERDICT_TRADING, VERDICT_CLOSED, VERDICT_UNKNOWN = "trading", "closed", "unknown"
SOURCE_WEEKEND, SOURCE_BLUEBOOK, SOURCE_KLINE, SOURCE_ASSUMED = (
    "weekend",
    "bluebook",
    "eastmoney-kline",
    "assumed",
)
DT_FORMAT = "%Y-%m-%d %H:%M:%S"

# 与 skills/portfolio-news/scripts/collect_news.py 的 TRADING_DAY_EDITION_TAGS / BLUEBOOK_NAME_MARK
# 同源同语义（该脚本用它们反推交易日集合，这里只用它们判「今日一条」）。**刻意不 import skill 脚本**：
# infra 不该依赖 skills 目录（skill 会被独立分发 / 换根），故允许常量语义重复实现；
# tests/test_market_calendar.py 有一条逐字相等断言，防止两边口径漂移。
TRADING_DAY_EDITION_TAGS = ("晨会版", "午间版")
BLUEBOOK_NAME_MARK = "蓝宝书"
# 版本词只出现在文件名标题段，正文混进文件名时不再向后找（与 collect_news 同值同理由）。
BLUEBOOK_NAME_HEAD = 60
_NAME_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_")


@dataclass(frozen=True)
class MarketVerdict:
    """一次门禁判定的完整留痕：结论 + 证据来源 + 备注 + 判定时刻（面板与日志据此解释）。"""

    market: str
    date: str
    verdict: str
    source: str
    note: str
    checked_at: str

    @property
    def trading(self) -> bool:
        """唯一放行口径：只有确凿闭市才拦住，unknown 一并放行。"""
        return self.verdict != VERDICT_CLOSED

    def to_state(self) -> dict[str, str]:
        """落盘/透传形状（值全为字符串，便于直接进 JSON 状态与模板）。"""
        return {
            "market": self.market,
            "date": self.date,
            "verdict": self.verdict,
            "source": self.source,
            "note": self.note,
            "checked_at": self.checked_at,
            "trading": "1" if self.trading else "0",
        }


def _date_from_name(name: str) -> date | None:
    """文件名形如 '2026-10-19_国内蓝宝书...'，取前 10 位日期（与 collect_news 同规则）。"""
    match = _NAME_DATE_RE.match(name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def bluebook_published_today(dirs: Iterable[Path], today: date) -> bool:
    """今日是否已有蓝宝书「晨会版 / 午间版」——这两种版本只在交易日刊出，可作开市证据。

    只读文件名（不读内容、不算整个交易日集合），几十个文件的 glob 成本可忽略。
    """
    for d in dirs:
        if not d.is_dir():
            continue
        for f in d.glob("*.md"):
            head = f.name[:BLUEBOOK_NAME_HEAD]
            if BLUEBOOK_NAME_MARK in head and any(tag in head for tag in TRADING_DAY_EDITION_TAGS):
                if _date_from_name(f.name) == today:
                    return True
    return False


def kline_state(market: str, *, today: date, timeout: int = KLINE_TIMEOUT_SECONDS) -> tuple[bool | None, str]:
    """东财日 K 尾部若干根，判断今日是否已在序列中。

    返回 (今日是否开盘, 备注)：True=序列含今日，False=序列最新日期早于今日（确凿闭市），
    None=取不到 / 空序列 / 最新日期晚于今日（本地时钟超前），**任何一种异常都不得退化成 False**
    ——把网络故障当闭市会让流程整天不跑。
    """
    secid = MARKETS[market]["secid"]
    # 只取尾部少量 K 线（lmt=10），不下全量：门禁只需看最新一行。
    url = f"{KLINE_API}?secid={secid}&klt=101&lmt=10&end=20500101"
    # Accept-Encoding: identity —— urllib 不自动解压，声明不压缩才能直接读明文；UA 是接口的最低门槛。
    request = Request(url, headers={"Accept-Encoding": "identity", "User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(request, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # 网络 / 超时 / 非 JSON：一律「取不到」，交回上层走 assumed
        return None, f"日K请求失败：{type(exc).__name__}: {exc}"

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None, "日K接口未返回 data（secid 或接口变更？）"
    klines = data.get("klines")
    if not isinstance(klines, list) or not klines:
        return None, "日K序列为空"

    latest = str(klines[-1]).split(",")[0].strip()
    try:
        latest_day = datetime.strptime(latest, "%Y-%m-%d").date()
    except ValueError:
        return None, f"日K日期无法解析：{latest!r}"
    if latest_day == today:
        return True, f"日K含今日（{latest}）"
    if latest_day < today:
        return False, f"日K最新一根停在 {latest}，早于今日 {today}"
    return None, f"日K最新一根为 {latest}，晚于今日 {today}（本地时钟可能超前）"


def market_gate(
    market: str = DEFAULT_MARKET,
    *,
    now: datetime | None = None,
    allow_network: bool = True,
    bluebook_dir: Path | None = None,
) -> MarketVerdict:
    """判定 market 今日是否开市；证据链见模块文档，首个决定性结论即返回。"""
    now = now or datetime.now()
    today = now.date()
    checked_at = now.strftime(DT_FORMAT)
    dirs: Iterable[Path] = (bluebook_dir,) if bluebook_dir is not None else BLUEBOOK_DIRS

    def verdict(value: str, source: str, note: str) -> MarketVerdict:
        return MarketVerdict(market, today.isoformat(), value, source, note, checked_at)

    # a) 周末：离线即可定论。
    if today.weekday() >= 5:
        return verdict(VERDICT_CLOSED, SOURCE_WEEKEND, "周末休市")

    # b) 蓝宝书当日刊出晨会版/午间版：离线证据，优先于触网。
    if bluebook_published_today(dirs, today):
        return verdict(VERDICT_TRADING, SOURCE_BLUEBOOK, "蓝宝书晨会版/午间版已刊出")

    # c) 日 K：只有开盘后「今日缺席」才可信，故盘前不触网（allow_network=False 时也不触网）。
    if allow_network and now.time() >= SESSION_OPEN_PROBE:
        opened, note = kline_state(market, today=today)
        if opened is True:
            return verdict(VERDICT_TRADING, SOURCE_KLINE, note)
        if opened is False:
            return verdict(VERDICT_CLOSED, SOURCE_KLINE, note)
        # opened is None → 不结论，落到 d)

    # d) 证据不足：保守放行，最坏退化成改动前的「不看日历」行为。
    return verdict(VERDICT_UNKNOWN, SOURCE_ASSUMED, "未经日历确认，保守放行")


def main() -> int:
    """python -m infra.market_calendar [市场] —— 打印结论便于人工观测/验收。"""
    market = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MARKET
    print(json.dumps(market_gate(market).to_state(), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
