"""交易日门禁：判定「今天这个市场是否开市」，供调度循环决定要不要干活。

为什么需要：调度原来只看钟点——周末与节假日到点照样抓一遍订阅、跑一遍 skill，白烧一次
浏览器与模型调用，产物还全是隔夜旧闻。把「今天是交易日吗」抽成一处判定，调度侧只消费
结论（放行 / 不放行），不自己拼证据。

证据链（首个决定性结论即返回）：
a) 本地交易日历（用户本机新浪解码产物，见下）→ 含今日即开市；**覆盖到今天之后却不含今日即闭市**；
b) 周末 → 闭市（离线、零成本，覆盖约 2/7 的天数）；
c) 蓝宝书「晨会版 / 午间版」当日已刊 → 开市（离线证据，见 bluebook_published_today）；
d) 东财日 K 序列含今日 → 开市；序列非空且最新日期早于今日 → 闭市（需网络，仅开盘后采信）；
e) 以上都取不到 → unknown，保守放行。

为什么本地日历放链首：它是唯一**盘前（<09:30）就能判闭市**的证据。日 K 在盘前不含今日行（当天还没
收盘），交易日与节假日盘前同形，故「今日缺席」只有开盘后才可采信；而一份**覆盖到今天之后**的日历里
「没有今天」本身就是确凿的休市证据——工作日节假日终于能在 8:10 门禁处拦下，不必白跑一次流程。
日历由用户本机流水线刷新（新浪 klc_td_sh → akshare 解码算法 → 交易日历.json）；本模块**只读不解码**，
刻意不把解码逻辑复制进本仓（那是用户已有的、独立演进的资产）。

放行口径只有一条：verdict != "closed"（unknown 也放行）。门禁的价值在于「确有证据证明闭市
时省一次流程」，而不是「证据不足时拦住流程」——证据不足时拦住会把网络抖动变成整天不跑。

不承担：盘中午休、半日市、港美股各自的节假日差异取证（港美股当前复用同一条链，仅 secid 不同）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
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
SOURCE_LOCAL_CALENDAR = "local-calendar"
DT_FORMAT = "%Y-%m-%d %H:%M:%S"

# 本地交易日历（用户本机资产，文件在本仓之外，故路径一律挂在环境变量上可覆盖）。
# DEFAULT_CALENDAR_FILE 与 DEFAULT_CALENDAR_UPDATER 指向用户自己的流水线目录；本仓不解释、
# 只读结果文件、只调它的更新脚本——新浪解码算法留在用户那边（SSOT），本仓不复制。
CALENDAR_FILE_ENV = "INFOFLOW_TRADING_CALENDAR_FILE"
DEFAULT_CALENDAR_FILE = Path(r"C:\业务流程\业务流程执行\交易日历.json")
CALENDAR_UPDATER_ENV = "INFOFLOW_TRADING_CALENDAR_UPDATER"
DEFAULT_CALENDAR_UPDATER = Path(r"C:\业务流程\业务流程执行\更新交易日历.js")
# 每月 10 日、20 日各刷一次（用户指定：日历覆盖到年底，一个月两刷足以吃掉假期表变动）。
CALENDAR_REFRESH_DAYS = (10, 20)
CALENDAR_REFRESH_TIMEOUT_SECONDS = 120
# 刷新失败时 stderr 进日志的截断长度：够定位原因，又不至于把整屏刷满。
CALENDAR_ERROR_TAIL = 200

# 与 skills/portfolio-news/scripts/collect_news.py 的 TRADING_DAY_EDITION_TAGS / BLUEBOOK_NAME_MARK
# 同源同语义（该脚本用它们反推交易日集合，这里只用它们判「今日一条」）。**刻意不 import skill 脚本**：
# infra 不该依赖 skills 目录（skill 会被独立分发 / 换根），故允许常量语义重复实现；
# tests/test_market_calendar.py 有一条逐字相等断言，防止两边口径漂移。
TRADING_DAY_EDITION_TAGS = ("晨会版", "午间版")
BLUEBOOK_NAME_MARK = "蓝宝书"
# 版本词只出现在文件名标题段，正文混进文件名时不再向后找（与 collect_news 同值同理由）。
BLUEBOOK_NAME_HEAD = 60
_NAME_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_")
# 本地日历的日期项形状（YYYY-MM-DD）：见 local_calendar_state 的信任边界校验。
_ISO_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


def local_calendar_file() -> Path:
    """本地交易日历路径：环境变量优先，否则用户本机流水线的默认落点。"""
    return Path(os.environ.get(CALENDAR_FILE_ENV) or DEFAULT_CALENDAR_FILE)


def local_calendar_state(path: Path, today: date) -> tuple[bool | None, str]:
    """读本地交易日历，判今日是否在册。

    三态：True=含今日（开市）；False=**日历覆盖到今天之后却不含今日**（确凿休市）；
    None=不可读或未覆盖今日（无结论，交回上层穿透到后续证据）。

    「覆盖充足时没有今天」是确凿闭市——这正是本地日历强于日 K 的地方：日 K 盘前不含今日行，
    交易日与休市日盘前同形，而日历带着未来日期，盘前就能判。反之日历若只到昨天（过期），
    「没有今天」只是没数据，绝不降级成闭市——把过期日历当休市会让流程整天不跑。
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # 缺失 / 权限 / 非法 JSON：一律不可读
        return None, f"本地交易日历不可读：{type(exc).__name__}: {exc}"
    days = payload.get("交易日") if isinstance(payload, dict) else None
    if not isinstance(days, list) or not days:
        return None, "本地交易日历不可读：交易日字段缺失或为空列表"
    updated = str((payload.get("更新时间") if isinstance(payload, dict) else "") or "未知时间")

    # 信任边界输入校验：日历是用户本机脚本的产物（可能被写坏/半截写入），而下面的比较是
    # **字典序**——非日期字符串（如 "zzz"）会排在 "2026-10-19" 之后，直接把交易日误判成休市。
    # 故先筛出合法 ISO 日期；一个都没有时退回「不可读」，绝不让垃圾数据产出一个闭市结论。
    valid = [str(day).strip() for day in days if _ISO_DAY_RE.match(str(day).strip())]
    if not valid:
        return None, "本地交易日历不可读：交易日字段里没有合法日期"
    iso = today.isoformat()
    if iso in valid:
        return True, f"本地交易日历含今日（更新于 {updated}）"
    latest = max(valid)  # 同格式下字典序即时序，省去逐条 strptime
    if latest >= iso:
        return False, f"本地交易日历覆盖至 {latest} 且不含今日 → 休市"
    return None, f"本地交易日历未覆盖今日（最大 {latest}，更新于 {updated}）"


def local_trading_days(path: Path | None = None) -> list[str]:
    """读本地交易日历里的合法日期（ISO 串，升序）。不可读/无合法日期返回空列表。

    与 local_calendar_state 共用同一份信任边界校验：日历是用户本机脚本的产物，
    非 ISO 串一律丢弃，避免字典序比较把垃圾数据当成日期。
    """
    try:
        payload = json.loads(Path(path or local_calendar_file()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    days = payload.get("交易日") if isinstance(payload, dict) else None
    if not isinstance(days, list):
        return []
    return sorted({str(day).strip() for day in days if _ISO_DAY_RE.match(str(day).strip())})


def is_last_trading_day_of_week(day: date, days: Iterable[str]) -> bool:
    """今天是所在 ISO 周最后一个交易日？

    日历覆盖到今天之后 → 看「下一个交易日是否落在下周」；覆盖只到本周末尾 → 退回周五。
    今天不在日历里（休市）一律 False——元组里的周五休市日不能因为「下一个交易日是下周一」而误判。
    完全没有日历时退回「周五」，宁可多跑一次，也不能整周不跑。
    """
    valid = [d for d in days if _ISO_DAY_RE.match(str(d))]
    iso = day.isoformat()
    if not valid:
        return day.weekday() == 4
    if iso not in valid:
        return False
    later = [d for d in valid if d > iso]
    if not later:
        return day.weekday() == 4
    nxt = date.fromisoformat(min(later))
    return nxt.isocalendar()[:2] != day.isocalendar()[:2]


def refresh_local_calendar(now: datetime | None = None) -> str:
    """按需调用用户本机的更新脚本刷新交易日历；返回一句中文说明，**永不抛异常**（调用方不设 try）。

    触发：今天是 CALENDAR_REFRESH_DAYS（10/20 号），或本地日历未覆盖今日（不可读 / 过期——
    对齐用户调度器「覆盖不足即拉取」的策略）。两者都不满足返回 ""，表示不动作。

    ponytail: 天花板是跨仓耦合——刷新用的是用户本机脚本与其输出文件，脚本路径 / 结构一变，
    本仓只会降级成「不刷新 + 门禁退回旧证据链」，不会崩（下面每一种失败都返回原因而非抛出）。
    升级路径：把新浪拉取与解码搬进本仓（或让用户调度器把日历落到本仓 config/），则耦合消除。
    """
    now = now or datetime.now()
    covered, _note = local_calendar_state(local_calendar_file(), now.date())
    # covered is False（日历覆盖充足且确认休市）不算「未覆盖」——日历本身是新的，无需刷。
    if now.day not in CALENDAR_REFRESH_DAYS and covered is not None:
        return ""

    node = shutil.which("node")
    if node is None:
        return "本地交易日历未刷新：找不到 node（日历保持原样，门禁降级到旧证据链）"
    script = Path(os.environ.get(CALENDAR_UPDATER_ENV) or DEFAULT_CALENDAR_UPDATER)
    if not script.is_file():
        return f"本地交易日历未刷新：找不到更新脚本 {script}（用户那边挪过位置？）"

    try:
        result = subprocess.run(
            [node, str(script)],
            cwd=script.parent,  # 脚本用 __dirname 定位输出文件与解码器，cwd 必须落在它自己目录
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CALENDAR_REFRESH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"本地交易日历未刷新：更新脚本超过 {CALENDAR_REFRESH_TIMEOUT_SECONDS} 秒未返回"
    except OSError as exc:  # node 不可执行 / cwd 消失等
        return f"本地交易日历未刷新：{type(exc).__name__}: {exc}"

    if result.returncode == 0:
        return "本地交易日历已刷新"
    tail = " ".join((result.stderr or "").split())[:CALENDAR_ERROR_TAIL]
    return f"本地交易日历未刷新：脚本退出码 {result.returncode}（{tail or '无 stderr'}）"


def market_gate(
    market: str = DEFAULT_MARKET,
    *,
    now: datetime | None = None,
    allow_network: bool = True,
    bluebook_dir: Path | None = None,
    calendar_file: Path | None = None,
) -> MarketVerdict:
    """判定 market 今日是否开市；证据链见模块文档，首个决定性结论即返回。

    bluebook_dir / calendar_file 是注入口（测试用）：只换证据位置，不改判定语义。
    """
    now = now or datetime.now()
    today = now.date()
    checked_at = now.strftime(DT_FORMAT)
    dirs: Iterable[Path] = (bluebook_dir,) if bluebook_dir is not None else BLUEBOOK_DIRS

    def verdict(value: str, source: str, note: str) -> MarketVerdict:
        return MarketVerdict(market, today.isoformat(), value, source, note, checked_at)

    # a) 本地交易日历：离线、盘前即可判闭市（日K 做不到），故排链首。
    covered, calendar_note = local_calendar_state(
        calendar_file if calendar_file is not None else local_calendar_file(), today
    )
    if covered is True:
        return verdict(VERDICT_TRADING, SOURCE_LOCAL_CALENDAR, calendar_note)
    if covered is False:
        return verdict(VERDICT_CLOSED, SOURCE_LOCAL_CALENDAR, calendar_note)
    # covered is None（不可读 / 过期）→ 穿透到后续证据

    # b) 周末：离线即可定论。
    if today.weekday() >= 5:
        return verdict(VERDICT_CLOSED, SOURCE_WEEKEND, "周末休市")

    # c) 蓝宝书当日刊出晨会版/午间版：离线证据，优先于触网。
    if bluebook_published_today(dirs, today):
        return verdict(VERDICT_TRADING, SOURCE_BLUEBOOK, "蓝宝书晨会版/午间版已刊出")

    # d) 日 K：只有开盘后「今日缺席」才可信，故盘前不触网（allow_network=False 时也不触网）。
    if allow_network and now.time() >= SESSION_OPEN_PROBE:
        opened, note = kline_state(market, today=today)
        if opened is True:
            return verdict(VERDICT_TRADING, SOURCE_KLINE, note)
        if opened is False:
            return verdict(VERDICT_CLOSED, SOURCE_KLINE, note)
        # opened is None → 不结论，落到 e)

    # e) 证据不足：保守放行，最坏退化成改动前的「不看日历」行为。
    return verdict(VERDICT_UNKNOWN, SOURCE_ASSUMED, "未经日历确认，保守放行")


def main() -> int:
    """python -m infra.market_calendar [市场] —— 打印结论便于人工观测/验收。"""
    market = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MARKET
    print(json.dumps(market_gate(market).to_state(), ensure_ascii=False, indent=1))
    # 本地日历单独打印一行：结论异常时先看这一行就知是「日历不可读/过期」还是「链上其它证据」。
    covered, note = local_calendar_state(local_calendar_file(), datetime.now().date())
    if covered is True:
        label = "含今日"
    elif covered is False:
        label = "覆盖充足但不含今日（休市）"
    else:
        label = "未定论（穿透后续证据）"
    print(f"本地交易日历 {local_calendar_file()}：{label} —— {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
