"""交易日门禁（infra/market_calendar.py）的证据链与防漂移回归检查。

全程离线：真实网络调用一律用 monkeypatch 替掉（本文件绝不发起真实请求）。
覆盖三条不变式：证据不足不得拦住流程（unknown 放行）、网络故障绝不能被当成闭市、
**不可读/过期的本地日历也必须穿透**（不得把「没数据」当休市）。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from datetime import date, datetime
from pathlib import Path

import pytest

from infra import market_calendar as mkt

MONDAY_PREOPEN = datetime(2026, 10, 19, 8, 20)  # 盘前：日K 尚无今日行，不能采信
MONDAY_OPENED = datetime(2026, 10, 19, 9, 31)
SATURDAY = datetime(2026, 10, 17, 10, 0)

LOCAL_CALENDAR_NAME = "交易日历.json"


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


@pytest.fixture(autouse=True)
def no_local_calendar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把本地日历指到 tmp 下一个**不存在**的文件（= 本地日历不可用）。

    本机真实日历覆盖到年底，若不断开，既有用例会被新链首条 a) 全部短路而假绿
    （周末/蓝宝书/日K 三条分支再也验不到）。用环境变量而非替掉 local_calendar_file()：
    这样准入口走的是真函数，环境变量覆盖本身也被顺带验到。
    需要本地日历的用例往这里返回的路径写文件即可。
    """
    path = tmp_path / LOCAL_CALENDAR_NAME
    monkeypatch.setenv(mkt.CALENDAR_FILE_ENV, str(path))
    return path


def _write_calendar(path: Path, days: list[str], updated: str = "2026/10/18 07:00:00") -> Path:
    """写一份用户本机流水线形状的日历（字段名与实测一致：更新时间/数据源/交易日）。"""
    path.write_text(
        json.dumps({"更新时间": updated, "数据源": "新浪财经 klc_td_sh", "交易日": days}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


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


# ── 本地交易日历：链首证据（三态） ────────────────────────────────


def test_local_calendar_containing_today_is_trading_even_preopen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    probe = _NoNetwork()
    monkeypatch.setattr(mkt, "kline_state", probe)
    path = _write_calendar(tmp_path / LOCAL_CALENDAR_NAME, ["2026-10-16", "2026-10-19", "2026-12-31"])
    verdict = mkt.market_gate(now=MONDAY_PREOPEN, calendar_file=path, bluebook_dir=tmp_path / "无此目录")
    assert (verdict.verdict, verdict.source) == (mkt.VERDICT_TRADING, mkt.SOURCE_LOCAL_CALENDAR)
    assert "含今日" in verdict.note and "2026/10/18" in verdict.note
    assert verdict.trading is True
    assert probe.calls == []


def test_local_calendar_covering_future_without_today_is_closed_preopen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """链首最核心的价值：覆盖到今天之后却不含今日 = 确凿休市，**盘前就能定论**。

    工作日节假日因此能在 8:20 门禁被拦下；日 K 在盘前做不到这件事（今日行尚未生成）。
    """
    probe = _NoNetwork()
    monkeypatch.setattr(mkt, "kline_state", probe)
    path = _write_calendar(tmp_path / LOCAL_CALENDAR_NAME, ["2026-10-16", "2026-10-20", "2026-12-31"])
    verdict = mkt.market_gate(now=MONDAY_PREOPEN, calendar_file=path)
    assert (verdict.verdict, verdict.source) == (mkt.VERDICT_CLOSED, mkt.SOURCE_LOCAL_CALENDAR)
    assert "2026-12-31" in verdict.note and "休市" in verdict.note
    assert verdict.trading is False
    assert probe.calls == []


def test_local_calendar_state_three_ways(tmp_path: Path) -> None:
    """三态直测：读不了 / 未覆盖今日 → None；含今日 → True；覆盖充足但不含今日 → False。"""
    today = date(2026, 10, 19)
    path = tmp_path / LOCAL_CALENDAR_NAME
    covered, note = mkt.local_calendar_state(path, today)
    assert covered is None and "不可读" in note

    path = _write_calendar(path, ["2026-10-19", "2026-12-31"])
    assert mkt.local_calendar_state(path, today)[0] is True

    path = _write_calendar(path, ["2026-10-16", "2026-12-31"])
    covered, note = mkt.local_calendar_state(path, today)
    assert covered is False and "休市" in note

    path = _write_calendar(path, ["2026-10-15", "2026-10-16"])
    covered, note = mkt.local_calendar_state(path, today)
    assert covered is None and "未覆盖今日" in note and "2026-10-16" in note


def test_garbage_entries_never_produce_false_closed(tmp_path: Path) -> None:
    """关键防线：非日期字符串（"zzz"）字典序排在 ISO 日期之后——不筛掉的话，
    「日历其实已过期」会被算成「覆盖充足且不含今日」，交易日整天不跑。"""
    today = date(2026, 10, 19)
    path = _write_calendar(tmp_path / LOCAL_CALENDAR_NAME, ["2026-10-15", "2026-10-16", "zzz"])
    covered, note = mkt.local_calendar_state(path, today)
    assert covered is None and "未覆盖今日" in note and "2026-10-16" in note

    # 合法日期含今日 → 照常 True，垃圾项不影响正向结论
    path = _write_calendar(path, ["2026-10-19", "2026-12-31", "zzz"])
    assert mkt.local_calendar_state(path, today)[0] is True


@pytest.mark.parametrize(
    "prepare",
    [
        pytest.param(lambda path: None, id="文件缺失"),
        pytest.param(lambda path: path.write_text("不是 JSON", encoding="utf-8"), id="JSON坏"),
        pytest.param(lambda path: path.write_text(json.dumps({"更新时间": "2026/10/18"}), encoding="utf-8"), id="交易日字段缺失"),
        pytest.param(lambda path: path.write_text(json.dumps({"交易日": []}), encoding="utf-8"), id="交易日空列表"),
        pytest.param(lambda path: path.write_text(json.dumps({"交易日": "2026-10-19"}), encoding="utf-8"), id="交易日不是列表"),
        pytest.param(lambda path: path.write_text(json.dumps({"交易日": ["不是日期", "zzz"]}), encoding="utf-8"), id="交易日全是垃圾"),
        pytest.param(lambda path: _write_calendar(path, ["2026-10-15", "2026-10-16"]), id="日历过期"),
    ],
)
def test_unusable_local_calendar_falls_through_to_next_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prepare
) -> None:  # noqa: ANN001
    """不可读 / 过期的日历一律穿透：不得把「没数据」降级成闭市（否则流程整天不跑）。"""
    probe = _NoNetwork()  # 盘前不该触网
    monkeypatch.setattr(mkt, "kline_state", probe)
    path = tmp_path / LOCAL_CALENDAR_NAME
    prepare(path)
    _bluebook(tmp_path, "2026-10-19_国内蓝宝书 10月19日晨会版 - 标题.md")
    verdict = mkt.market_gate(now=MONDAY_PREOPEN, calendar_file=path, bluebook_dir=tmp_path)
    assert (verdict.verdict, verdict.source) == (mkt.VERDICT_TRADING, mkt.SOURCE_BLUEBOOK)
    assert probe.calls == []


def test_local_calendar_file_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(mkt.CALENDAR_FILE_ENV, raising=False)
    assert mkt.local_calendar_file() == mkt.DEFAULT_CALENDAR_FILE

    override = _write_calendar(tmp_path / "别处的日历.json", ["2026-10-16", "2026-10-19", "2026-12-31"])
    monkeypatch.setenv(mkt.CALENDAR_FILE_ENV, str(override))
    assert mkt.local_calendar_file() == override
    # 覆盖路径要真的驱动门禁，不只是给个路径
    verdict = mkt.market_gate(now=MONDAY_PREOPEN)
    assert (verdict.verdict, verdict.source) == (mkt.VERDICT_TRADING, mkt.SOURCE_LOCAL_CALENDAR)


# ── 刷新：只在 10/20 号或日历过期时动作，且永不抛异常 ──────────────


class _Completed:
    """subprocess.run 的最小替身：只带被测代码读的两个字段。"""

    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr


def _stub_refresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    result: _Completed | None = None,
    exc: Exception | None = None,
) -> tuple[list[tuple[list[str], dict]], Path]:
    """打桩 node / 更新脚本 / subprocess.run，返回 (调用记录, 脚本路径)。

    刻意替掉 subprocess.run 与 shutil.which 本身（而不是替掉 refresh_local_calendar）：
    要验的正是「怎么调、cwd 对不对、失败怎么收敛」。
    """
    script = tmp_path / "更新交易日历.js"
    script.write_text("// stub", encoding="utf-8")
    monkeypatch.setenv(mkt.CALENDAR_UPDATER_ENV, str(script))
    monkeypatch.setattr(mkt.shutil, "which", lambda name: "node" if name == "node" else None)
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv: list[str], **kwargs) -> _Completed:  # noqa: ANN003
        calls.append((argv, kwargs))
        if exc is not None:
            raise exc
        return result if result is not None else _Completed()

    monkeypatch.setattr(mkt.subprocess, "run", fake_run)
    return calls, script


def test_refresh_does_nothing_on_quiet_day_when_calendar_is_current(
    no_local_calendar: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_calendar(no_local_calendar, ["2026-10-21", "2026-12-31"])  # 覆盖今日且非休市
    calls, _script = _stub_refresh(monkeypatch, tmp_path)
    assert mkt.refresh_local_calendar(datetime(2026, 10, 21, 7, 10)) == ""
    assert calls == []  # 不动作时连 subprocess 都不碰


def test_refresh_runs_on_refresh_day(
    no_local_calendar: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_calendar(no_local_calendar, ["2026-10-20", "2026-12-31"])  # 日历本身是新的，仍按日子刷
    calls, script = _stub_refresh(monkeypatch, tmp_path)
    note = mkt.refresh_local_calendar(datetime(2026, 10, 20, 7, 10))
    assert "已刷新" in note
    [(argv, kwargs)] = calls
    assert argv == ["node", str(script)]
    assert kwargs["cwd"] == script.parent  # 脚本用 __dirname 定位输出文件，cwd 必须对
    assert kwargs["timeout"] == mkt.CALENDAR_REFRESH_TIMEOUT_SECONDS
    assert kwargs["capture_output"] is True and kwargs["encoding"] == "utf-8"


def test_refresh_runs_when_calendar_expired_even_on_quiet_day(
    no_local_calendar: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """过期补刷一次：对齐用户调度器「覆盖不足即拉取」的策略。"""
    _write_calendar(no_local_calendar, ["2026-10-15", "2026-10-16"])
    calls, _script = _stub_refresh(monkeypatch, tmp_path)
    note = mkt.refresh_local_calendar(datetime(2026, 10, 19, 7, 10))  # 19 号不是刷新日
    assert "已刷新" in note and len(calls) == 1


def test_refresh_without_node_returns_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mkt.shutil, "which", lambda _name: None)
    monkeypatch.setattr(mkt.subprocess, "run", lambda *a, **kw: pytest.fail("找不到 node 时不该执行脚本"))
    note = mkt.refresh_local_calendar(datetime(2026, 10, 20, 7, 10))
    assert "未刷新" in note and "node" in note


def test_refresh_without_updater_script_returns_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(mkt.CALENDAR_UPDATER_ENV, str(tmp_path / "不存在.js"))
    monkeypatch.setattr(mkt.shutil, "which", lambda _name: "node")
    monkeypatch.setattr(mkt.subprocess, "run", lambda *a, **kw: pytest.fail("脚本缺失时不该执行"))
    note = mkt.refresh_local_calendar(datetime(2026, 10, 20, 7, 10))
    assert "找不到更新脚本" in note


@pytest.mark.parametrize(
    "exc",
    [
        subprocess.TimeoutExpired(cmd="node", timeout=120),
        OSError("cwd 不见了"),
    ],
)
def test_refresh_survives_subprocess_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    """契约：永不抛异常。超时 / 起不来都只返回一句说明，抓取槽照常往下走。"""
    _stub_refresh(monkeypatch, tmp_path, exc=exc)
    note = mkt.refresh_local_calendar(datetime(2026, 10, 20, 7, 10))
    assert "未刷新" in note


def test_refresh_reports_failure_with_truncated_stderr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_refresh(monkeypatch, tmp_path, result=_Completed(1, "[失败] 解码结果异常\n" + "x" * 500))
    note = mkt.refresh_local_calendar(datetime(2026, 10, 20, 7, 10))
    assert "退出码 1" in note and "解码结果异常" in note
    assert len(note) < 300  # stderr 截断：日志里够定位原因，不刷屏

