"""交易日门禁（infra/market_calendar.py）的证据链与防漂移回归检查。

全程离线：真实网络调用一律用 monkeypatch 替掉（本文件绝不发起真实请求）。
覆盖两条不变式：证据不足不得拦住流程（unknown 放行），网络故障绝不能被当成闭市。
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from infra import market_calendar as mkt

MONDAY_PREOPEN = datetime(2026, 10, 19, 8, 20)  # 盘前：日K 尚无今日行，不能采信
MONDAY_OPENED = datetime(2026, 10, 19, 9, 31)
SATURDAY = datetime(2026, 10, 17, 10, 0)


class _NoNetwork:
    """替掉 kline_state 的探针：被调用即记录；测试用 ``calls == []`` 证明「未触网」。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, market: str, *, today: date, timeout: int = 0):  # noqa: ANN204
        self.calls.append(market)
        raise AssertionError("盘前/无需触网的场景不该查日K")


def _bluebook(tmp_path: Path, name: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / name).write_text("x", encoding="utf-8")
    return tmp_path


def test_weekend_is_closed_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = _NoNetwork()
    monkeypatch.setattr(mkt, "kline_state", probe)
    verdict = mkt.market_gate(now=SATURDAY, allow_network=True)
    assert (verdict.verdict, verdict.source) == (mkt.VERDICT_CLOSED, mkt.SOURCE_WEEKEND)
    assert verdict.trading is False
    assert probe.calls == []


def test_bluebook_today_is_trading_without_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    probe = _NoNetwork()
    monkeypatch.setattr(mkt, "kline_state", probe)
    _bluebook(tmp_path, "2026-10-19_国内蓝宝书 10月19日晨会版 - 标题.md")
    verdict = mkt.market_gate(now=MONDAY_OPENED, bluebook_dir=tmp_path)
    assert (verdict.verdict, verdict.source) == (mkt.VERDICT_TRADING, mkt.SOURCE_BLUEBOOK)
    assert verdict.trading is True
    assert probe.calls == []


def test_bluebook_other_day_or_edition_is_not_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    probe = _NoNetwork()
    monkeypatch.setattr(mkt, "kline_state", probe)
    # 昨日晨会版 + 今日晚间版（晚间版周末也发，不算交易日证据）
    _bluebook(tmp_path, "2026-10-16_国内蓝宝书 10月16日晨会版 - 标题.md")
    _bluebook(tmp_path, "2026-10-19_国内蓝宝书 10月19日晚间版 - 标题.md")
    verdict = mkt.market_gate(now=MONDAY_PREOPEN, bluebook_dir=tmp_path)
    assert verdict.verdict == mkt.VERDICT_UNKNOWN
    assert probe.calls == []


def test_preopen_without_evidence_is_unknown_and_never_touches_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = _NoNetwork()
    monkeypatch.setattr(mkt, "kline_state", probe)
    verdict = mkt.market_gate(now=MONDAY_PREOPEN, allow_network=True, bluebook_dir=tmp_path)
    assert (verdict.verdict, verdict.source) == (mkt.VERDICT_UNKNOWN, mkt.SOURCE_ASSUMED)
    # unknown 也放行：证据不足不得拦住流程
    assert verdict.trading is True
    assert probe.calls == []


def test_allow_network_false_skips_kline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    probe = _NoNetwork()
    monkeypatch.setattr(mkt, "kline_state", probe)
    verdict = mkt.market_gate(now=MONDAY_OPENED, allow_network=False, bluebook_dir=tmp_path)
    assert verdict.verdict == mkt.VERDICT_UNKNOWN
    assert probe.calls == []


def test_kline_containing_today_is_trading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake(market: str, *, today: date, timeout: int = 0):  # noqa: ANN202
        seen.append(market)
        return True, "日K含今日（2026-10-19）"

    monkeypatch.setattr(mkt, "kline_state", fake)
    verdict = mkt.market_gate(now=MONDAY_OPENED, bluebook_dir=tmp_path)
    assert (verdict.verdict, verdict.source) == (mkt.VERDICT_TRADING, mkt.SOURCE_KLINE)
    assert seen == ["A"]


def test_kline_without_today_is_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mkt, "kline_state", lambda market, *, today, timeout=0: (False, "日K最新一根停在 2026-10-16"))
    verdict = mkt.market_gate(now=MONDAY_OPENED, bluebook_dir=tmp_path)
    assert (verdict.verdict, verdict.source) == (mkt.VERDICT_CLOSED, mkt.SOURCE_KLINE)


@pytest.mark.parametrize("state", [(None, "取不到"), (None, "日K序列为空"), (None, "晚于今日")])
def test_kline_undecided_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: tuple) -> None:
    monkeypatch.setattr(mkt, "kline_state", lambda market, *, today, timeout=0: state)
    verdict = mkt.market_gate(now=MONDAY_OPENED, bluebook_dir=tmp_path)
    assert (verdict.verdict, verdict.source) == (mkt.VERDICT_UNKNOWN, mkt.SOURCE_ASSUMED)
    assert verdict.trading is True


@pytest.mark.parametrize("market", ["H", "US"])
def test_gate_chain_works_for_other_markets(
    market: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def fake(m: str, *, today: date, timeout: int = 0):  # noqa: ANN202
        seen.append(m)
        return True, "日K含今日"

    monkeypatch.setattr(mkt, "kline_state", fake)
    verdict = mkt.market_gate(market, now=MONDAY_OPENED, bluebook_dir=tmp_path)
    assert (verdict.market, verdict.verdict, verdict.source) == (market, mkt.VERDICT_TRADING, mkt.SOURCE_KLINE)
    assert seen == [market]
    # 周末分支同样按市场无关处理
    assert mkt.market_gate(market, now=SATURDAY).verdict == mkt.VERDICT_CLOSED


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _kline_via(urlopen_return: object, monkeypatch: pytest.MonkeyPatch, market: str = "A") -> tuple:
    urls: list[str] = []

    def fake_urlopen(request, timeout=None):  # noqa: ANN001, ANN202
        urls.append(request.full_url)
        if isinstance(urlopen_return, Exception):
            raise urlopen_return
        return urlopen_return

    monkeypatch.setattr(mkt, "urlopen", fake_urlopen)
    result = mkt.kline_state(market, today=date(2026, 10, 19))
    return result, urls


def test_kline_state_builds_tail_only_url_with_identity_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, dict]] = []

    def fake_urlopen(request, timeout=None):  # noqa: ANN001, ANN202
        seen.append((request.full_url, dict(request.headers)))
        body = json.dumps({"data": {"klines": ["2026-10-16,1,2", "2026-10-19,1,2"]}}).encode("utf-8")
        return _FakeResponse(body)

    monkeypatch.setattr(mkt, "urlopen", fake_urlopen)
    opened, note = mkt.kline_state("H", today=date(2026, 10, 19))
    assert opened is True
    assert "2026-10-19" in note
    url, headers = seen[0]
    assert "secid=100.HSI" in url and "lmt=10" in url and "end=20500101" in url
    # urllib 不自动解压：必须声明 identity 才能直接读明文
    assert headers.get("Accept-encoding") == "identity"


@pytest.mark.parametrize(
    "body",
    [
        {"data": None},
        {"data": {"klines": []}},
        {"data": {"klines": ["2026-10-20,1,2"]}},  # 最新日期晚于今日 = 本地时钟超前，不可采信
        {},
    ],
)
def test_kline_state_undecided_cases_never_become_closed(body: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps(body).encode("utf-8")
    (opened, _note), _urls = _kline_via(_FakeResponse(payload), monkeypatch)
    assert opened is None


def test_kline_state_earlier_latest_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps({"data": {"klines": ["2026-10-15,1,2", "2026-10-16,1,2"]}}).encode("utf-8")
    (opened, note), _urls = _kline_via(_FakeResponse(payload), monkeypatch)
    assert opened is False
    assert "2026-10-16" in note


def test_kline_state_http_error_or_bad_json_is_undecided(monkeypatch: pytest.MonkeyPatch) -> None:
    (opened, note), _urls = _kline_via(RuntimeError("connection reset"), monkeypatch)
    assert opened is None and "RuntimeError" in note

    (opened, note), _urls = _kline_via(_FakeResponse(b"<html>502</html>"), monkeypatch)
    assert opened is None and note


def test_bluebook_published_today_reads_names_only(tmp_path: Path) -> None:
    _bluebook(tmp_path, "2026-10-19_国内蓝宝书 10月19日晨会版 - 标题.md")
    assert mkt.bluebook_published_today((tmp_path,), date(2026, 10, 19)) is True
    assert mkt.bluebook_published_today((tmp_path,), date(2026, 10, 18)) is False
    assert mkt.bluebook_published_today((tmp_path / "缺失",), date(2026, 10, 19)) is False
    # 日期后缀带「午间版」但文件名日期不是今日 → 不是今日证据
    _bluebook(tmp_path, "2026-10-16_国内蓝宝书 10月16日午间版 - 标题.md")
    assert mkt.bluebook_published_today((tmp_path,), date(2026, 10, 16)) is True


def test_to_state_shape_is_all_strings() -> None:
    verdict = mkt.market_gate(now=SATURDAY)
    state = verdict.to_state()
    assert set(state) == {"market", "date", "verdict", "source", "note", "checked_at", "trading"}
    assert all(isinstance(value, str) for value in state.values())
    assert state["trading"] == "0"


def test_trading_day_edition_tags_match_collect_news_script() -> None:
    """防口径漂移：本模块与 collect_news.py 的交易日版本标记必须逐字相等。

    选 importlib 按路径加载而非正则读源码：要比较的是常量**真值**（正则只会拿到源码字面量，
    拼接/转义写法下会假绿），且该脚本模块级只有常量与函数定义，导入无副作用。
    """
    script = Path(mkt.BASE_DIR) / "skills" / "portfolio-news" / "scripts" / "collect_news.py"
    spec = importlib.util.spec_from_file_location("_collect_news_for_drift_check", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert mkt.TRADING_DAY_EDITION_TAGS == module.TRADING_DAY_EDITION_TAGS
    assert mkt.BLUEBOOK_NAME_MARK == module.BLUEBOOK_NAME_MARK
