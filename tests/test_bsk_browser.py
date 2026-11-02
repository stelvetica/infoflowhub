from __future__ import annotations

import json
import sys
import time
from types import SimpleNamespace

import pytest

from connectors._shared import bsk_browser
from connectors._shared.bsk_browser import (
    BskBrowser,
    BskCommandError,
    BskHelpTimeout,
    BskSessionError,
    BskTimeout,
)


# 假 bsk CLI：不依赖真实 daemon/Chrome，行为由 FAKE_BSK_SCENARIO 指向的 JSON 文件驱动。
# 每次调用都把 argv 与 BSK_AUTO_START 追加到 log_path，供断言命令形状与生命周期。
FAKE_CLI = '''
import json, os, sys, time

argv = sys.argv[1:]
scenario_path = os.environ.get("FAKE_BSK_SCENARIO", "")
scenario = json.loads(open(scenario_path, encoding="utf-8").read()) if scenario_path else {}
log_path = scenario.get("log_path")
if log_path:
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": argv, "auto_start": os.environ.get("BSK_AUTO_START")}, ensure_ascii=False) + "\\n")

secret = scenario.get("stderr_secret")
if secret:
    sys.stderr.write("token=" + secret + "\\n")

cmd = argv[0] if argv else ""

if cmd == "status":
    # 默认模拟「daemon 就绪且已连浏览器」；场景可用 status_exit/status_stdout 覆盖
    # （如 status_exit=2 + status_stdout={"message": "..."} 模拟 daemon 不在）。
    sys.stdout.write(
        scenario.get("status_stdout", json.dumps(scenario.get("status", {"browsers": [{"instance_id": "b1"}]})))
    )
    sys.exit(scenario.get("status_exit", 0))

if cmd == "daemon" and len(argv) > 1 and argv[1] == "start":
    sys.stdout.write(scenario.get("daemon_stdout", "daemon started"))
    sys.exit(scenario.get("daemon_exit", 0))

if cmd == "session" and len(argv) > 1 and argv[1] == "start":
    time.sleep(scenario.get("start_sleep", 0))
    sys.stdout.write(scenario.get("start_stdout", '{"session_id": "fake-session"}'))
    sys.exit(scenario.get("start_exit", 0))

if cmd == "session" and len(argv) > 1 and argv[1] == "list":
    sys.stdout.write(json.dumps(scenario.get("list_sessions", []), ensure_ascii=False))
    sys.exit(scenario.get("list_exit", 0))

if cmd == "session" and len(argv) > 1 and argv[1] == "stop":
    time.sleep(scenario.get("stop_sleep", 0))
    exit_code = scenario.get("stop_exit", 0)
    seq = scenario.get("stop_exit_seq")
    if seq:
        # 本次调用已在日志里落过一行，故「已记录的 stop 次数 - 1」就是本次的序号。
        prior = 0
        if log_path and os.path.exists(log_path):
            with open(log_path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        if json.loads(line).get("argv", [])[0:2] == ["session", "stop"]:
                            prior += 1
                    except ValueError:
                        pass
        exit_code = seq[min(prior - 1, len(seq) - 1)]
    sys.stdout.write("stopped " + (argv[2] if len(argv) > 2 else ""))
    sys.exit(exit_code)

if cmd == "cooperate" and len(argv) > 1 and argv[1] == "sync":
    # bsk 0.4+ 动作前的控制上下文；scenario 可覆盖成失败信封，验证重试/报错路径。
    sys.stdout.write(
        json.dumps(
            scenario.get(
                "cooperate_sync",
                {
                    "context_id": "ctx-fake",
                    "observation": {"observation_id": "obs-fake"},
                    "tab_id": 1,
                },
            ),
            ensure_ascii=False,
        )
    )
    sys.exit(scenario.get("cooperate_sync_exit", 0))

if cmd == "evaluate":
    time.sleep(scenario.get("evaluate_sleep", 0))
    expr = argv[-1]
    for key, envelope in scenario.get("evaluate", {}).items():
        if key in expr:
            sys.stdout.write(json.dumps(envelope, ensure_ascii=False))
            sys.exit(scenario.get("evaluate_exit", 0))
    sys.stdout.write(json.dumps(scenario.get("evaluate_default", {"ok": True, "value": None}), ensure_ascii=False))
    sys.exit(scenario.get("evaluate_exit", 0))

if cmd == "navigate":
    if "--context" not in argv and scenario.get("navigate_requires_context"):
        # 复刻 bsk 0.4+：会话里已执行过动作后，无上下文的导航被立即拒绝（effect_state=none）。
        sys.stdout.write(
            json.dumps(
                {
                    "code": "permission_denied",
                    "message": "This action requires the context returned by the latest cooperate sync",
                    "exit_code": 1,
                    "data": {"effect_state": "none", "reason": "cooperation_sync_required"},
                },
                ensure_ascii=False,
            )
        )
        sys.exit(1)
    sys.stdout.write(scenario.get("navigate_stdout", "tab=1 reached=load\\nurl=" + argv[1]))
    sys.exit(scenario.get("navigate_exit", 0))

if cmd == "get-html":
    sys.stdout.write(scenario.get("html", "<html></html>"))
    sys.exit(scenario.get("html_exit", 0))

if cmd == "wait-ms":
    time.sleep(scenario.get("wait_ms_sleep", 0))
    sys.stdout.write("slept " + (argv[1] if len(argv) > 1 else ""))
    sys.exit(scenario.get("wait_ms_exit", 0))

if cmd == "wheel":
    time.sleep(scenario.get("wheel_sleep", 0))
    sys.stdout.write("wheeled")
    sys.exit(scenario.get("wheel_exit", 0))

sys.stdout.write("unknown")
sys.exit(0)
'''


@pytest.fixture
def fake_bsk(tmp_path, monkeypatch):
    script = tmp_path / "fake_bsk.py"
    script.write_text(FAKE_CLI, encoding="utf-8")
    scenario_path = tmp_path / "scenario.json"
    log_path = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_BSK_SCENARIO", str(scenario_path))

    def configure(**scenario):
        scenario["log_path"] = str(log_path)
        scenario_path.write_text(json.dumps(scenario, ensure_ascii=False), encoding="utf-8")

    def build(**scenario):
        ctor = {k: scenario.pop(k) for k in list(scenario) if k in ("timeout", "request_help_timeout")}
        configure(**scenario)
        logs: list[str] = []
        browser = BskBrowser(command=(sys.executable, str(script)), logger=logs.append, **ctor)
        return browser, logs

    def calls():
        if not log_path.exists():
            return []
        return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def evaluate_exprs():
        return [c["argv"][-1] for c in calls() if c["argv"] and c["argv"][0] == "evaluate"]

    return SimpleNamespace(build=build, calls=calls, configure=configure, evaluate_exprs=evaluate_exprs)


# 注意：假 CLI 记录的是 bsk 层级 token（已去掉解释器与脚本路径），argv[0] 即子命令。
def _stops(fake_bsk):
    return [c for c in fake_bsk.calls() if c["argv"][0:2] == ["session", "stop"]]


def _subcommand_calls(fake_bsk, name):
    return [c for c in fake_bsk.calls() if c["argv"] and c["argv"][0] == name]


# ---- 会话生命周期 ----


def test_start_parses_session_id(fake_bsk):
    browser, _ = fake_bsk.build(start_stdout='{"session_id": "s-123", "browser_instance_id": "b1"}')
    assert browser.start() == "s-123"
    assert browser.session_id == "s-123"
    # 起会话前先探一次 daemon（自愈入口），再发 session start
    assert fake_bsk.calls()[0]["argv"][0:2] == ["status", "--json"]
    session_calls = [c for c in fake_bsk.calls() if c["argv"][0:3] == ["session", "start", "--json"]]
    assert session_calls, "未发出 session start"


def test_every_command_disables_auto_start(fake_bsk):
    browser, _ = fake_bsk.build()
    browser.start()
    browser.page().goto("https://example.com")
    browser.stop()
    assert fake_bsk.calls()
    assert all(c["auto_start"] == "0" for c in fake_bsk.calls())


def test_start_failure_leaves_no_session(fake_bsk):
    browser, _ = fake_bsk.build(start_exit=1, start_stdout="error: no browser connected")
    with pytest.raises(BskSessionError):
        browser.start()
    assert browser.session_id is None
    assert _stops(fake_bsk) == []


def test_start_failure_with_session_id_stops_it(fake_bsk):
    browser, _ = fake_bsk.build(start_exit=1, start_stdout='{"session_id": "s-bad"}')
    with pytest.raises(BskSessionError):
        browser.start()
    assert browser.session_id is None
    assert len(_stops(fake_bsk)) == 1


def test_start_without_session_id_raises(fake_bsk):
    browser, _ = fake_bsk.build(start_stdout='{"browser_instance_id": "b1"}')
    with pytest.raises(BskSessionError):
        browser.start()
    assert browser.session_id is None


def test_stop_uses_positional_session_id_and_is_idempotent(fake_bsk):
    browser, _ = fake_bsk.build(start_stdout='{"session_id": "s-9"}')
    browser.start()
    browser.stop()
    browser.stop()
    stops = _stops(fake_bsk)
    assert len(stops) == 1
    assert stops[0]["argv"][2] == "s-9"
    assert "--session" not in stops[0]["argv"]


def test_stop_swallows_failure(fake_bsk):
    browser, _ = fake_bsk.build(stop_exit=1)
    browser.start()
    browser.stop()  # 收尾路径不得再抛
    assert browser.session_id is None


def test_stop_runs_on_business_exception(fake_bsk):
    browser, _ = fake_bsk.build()
    with pytest.raises(RuntimeError):
        with browser:
            raise RuntimeError("业务炸了")
    assert browser.session_id is None
    assert len(_stops(fake_bsk)) == 1


def test_page_op_without_session_raises(fake_bsk):
    browser, _ = fake_bsk.build()
    with pytest.raises(BskSessionError):
        browser.page().url


# ---- 超时与错误分类 ----


def test_subprocess_timeout_is_distinct_error(fake_bsk):
    browser, logs = fake_bsk.build(evaluate_sleep=5, timeout=0.4, request_help_timeout=None)
    browser.start()
    with pytest.raises(BskTimeout) as excinfo:
        browser.evaluate("1+1")
    assert not isinstance(excinfo.value, BskHelpTimeout)
    # 超时后仍要尝试 stop session
    assert browser.session_id is None
    assert len(_stops(fake_bsk)) == 1
    assert any("err=BskTimeout" in line for line in logs)


def test_request_help_watchdog_classifies_help_timeout(fake_bsk):
    assert issubclass(BskHelpTimeout, BskTimeout)
    browser, logs = fake_bsk.build(evaluate_sleep=5, timeout=30.0, request_help_timeout=0.4)
    browser.start()
    with pytest.raises(BskHelpTimeout):
        browser.evaluate("1+1")
    assert len(_stops(fake_bsk)) == 1
    assert any("err=BskHelpTimeout" in line for line in logs)


def test_bsk_internal_timeout_maps_to_bsk_timeout(fake_bsk):
    browser, _ = fake_bsk.build(
        evaluate_exit=4,
        evaluate_default={"code": "timeout", "message": "tool RPC timed out after 2s", "exit_code": 4},
    )
    browser.start()
    with pytest.raises(BskTimeout) as excinfo:
        browser.evaluate("1+1")
    assert not isinstance(excinfo.value, BskHelpTimeout)


def test_bsk_error_exit_maps_to_command_error(fake_bsk):
    browser, _ = fake_bsk.build(
        navigate_exit=1,
        navigate_stdout='{"code": "not_found", "message": "session not registered", "exit_code": 1}',
    )
    browser.start()
    with pytest.raises(BskCommandError):
        browser.page().goto("https://example.com")


def test_evaluate_js_error_raises_command_error(fake_bsk):
    browser, _ = fake_bsk.build(evaluate_default={"ok": False, "error": {"text": "Error: boom"}})
    browser.start()
    with pytest.raises(BskCommandError) as excinfo:
        browser.evaluate("throw 1")
    assert "boom" in str(excinfo.value)


# ---- bsk 0.4+ 的 cooperate sync 契约 ----


def test_evaluate_syncs_first_and_passes_context(fake_bsk):
    """协议 1.8 起动作必须带最新 sync 的上下文，一次 context 只授权一次动作。"""
    browser, _ = fake_bsk.build()
    browser.start()
    assert browser.evaluate("1+1") is None
    assert browser.evaluate("2+2") is None
    calls = fake_bsk.calls()
    action_calls = [c["argv"] for c in calls if c["argv"][0] in ("cooperate", "evaluate")]
    # 每个动作前各有一次 sync，顺序必须是 sync → evaluate
    assert [argv[0] for argv in action_calls] == ["cooperate", "evaluate", "cooperate", "evaluate"]
    for argv in action_calls[1::2]:
        assert argv[argv.index("--context") + 1] == "ctx-fake"
        assert argv[argv.index("--observation") + 1] == "obs-fake"
    # sync 自身也要带 strict 策略与 session
    for argv in action_calls[0::2]:
        assert argv[1:2] == ["sync"]
        assert "--policy" in argv and argv[argv.index("--policy") + 1] == "strict"


def test_wheel_syncs_and_passes_context(fake_bsk):
    browser, _ = fake_bsk.build()
    browser.start()
    browser.wheel(0, 120)
    calls = [c["argv"] for c in fake_bsk.calls() if c["argv"][0] in ("cooperate", "wheel")]
    assert [argv[0] for argv in calls] == ["cooperate", "wheel"]
    assert calls[1][calls[1].index("--context") + 1] == "ctx-fake"


def test_sync_failure_raises_instead_of_acting_blind(fake_bsk):
    """sync 拿不到上下文时必须报错，不能退化成无上下文直接动作（那必然 permission_denied）。"""
    browser, _ = fake_bsk.build(
        cooperate_sync={"code": "permission_denied", "message": "Page changed while synchronizing"},
        cooperate_sync_exit=1,
    )
    browser.start()
    with pytest.raises(BskCommandError) as excinfo:
        browser.evaluate("1+1")
    assert "cooperate sync" in str(excinfo.value)
    assert fake_bsk.evaluate_exprs() == []


# ---- locator 契约 ----


def test_locator_count_nth_first_inner_text(fake_bsk):
    browser, _ = fake_bsk.build(
        evaluate={
            ".length": {"ok": True, "value": 3},
            "return e?e.innerText:null": {"ok": True, "value": "第三条"},
        }
    )
    browser.start()
    locator = browser.page().locator(".item")
    assert locator.count() == 3
    assert locator.first.inner_text() == "第三条"
    assert locator.nth(2).inner_text() == "第三条"
    exprs = fake_bsk.evaluate_exprs()
    assert "els[2]" in exprs[-1]
    assert "els[0]" in exprs[-2]


def test_locator_all_inner_texts_single_roundtrip(fake_bsk):
    browser, _ = fake_bsk.build(evaluate={".map(e=>e.innerText)": {"ok": True, "value": ["甲", "乙"]}})
    browser.start()
    assert browser.page().locator(".item").all_inner_texts() == ["甲", "乙"]
    assert len(fake_bsk.evaluate_exprs()) == 1


def test_locator_missing_element_returns_empty_text(fake_bsk):
    browser, _ = fake_bsk.build(evaluate={"return e?e.innerText:null": {"ok": True}})
    browser.start()
    assert browser.page().locator(".nope").inner_text() == ""


def test_locator_get_by_text_exact_plumbing(fake_bsk):
    browser, _ = fake_bsk.build(evaluate={"return e?e.innerText:null": {"ok": True, "value": "标题一"}})
    browser.start()
    locator = browser.page().locator(".list-box").get_by_text("标题一", exact=True).first
    assert locator.inner_text() == "标题一"
    expr = fake_bsk.evaluate_exprs()[-1]
    assert 'const sel=".list-box"' in expr
    assert f"const text={json.dumps('标题一')}" in expr
    assert "const exact=true" in expr
    assert "els[0]" in expr


def test_page_get_by_text_without_selector(fake_bsk):
    browser, _ = fake_bsk.build(evaluate={".length": {"ok": True, "value": 1}})
    browser.start()
    assert browser.page().get_by_text("标题", exact=True).count() == 1
    expr = fake_bsk.evaluate_exprs()[-1]
    assert 'const sel=null' in expr
    # 无选择器时从 body 起扫，避免 head 里的 <title> 被文本匹配命中
    assert "(document.body||document.documentElement).querySelectorAll('*')" in expr


def test_locator_click_uses_evaluate_not_observe(fake_bsk):
    browser, _ = fake_bsk.build(evaluate={"scrollIntoView": {"ok": True, "value": "CLICKED"}})
    browser.start()
    browser.page().locator(".item").first.click()
    expr = fake_bsk.evaluate_exprs()[-1]
    assert "scrollIntoView" in expr and "els[0]" in expr
    # 点击不依赖 @eN 引用，因此不产生 observe/get-html 往返
    assert not any(c["argv"][0] in ("observe", "get-html") for c in fake_bsk.calls())


def test_locator_click_miss_raises(fake_bsk):
    browser, _ = fake_bsk.build(evaluate={"scrollIntoView": {"ok": True, "value": "NO_MATCH"}})
    browser.start()
    with pytest.raises(BskCommandError):
        browser.page().locator(".item").click()


# ---- page facade ----


def test_page_url_content_close(fake_bsk):
    browser, _ = fake_bsk.build(
        evaluate={"location.href": {"ok": True, "value": "https://example.com/"}},
        html="<html>hi</html>",
    )
    browser.start()
    page = browser.page()
    assert page.url == "https://example.com/"
    assert page.content() == "<html>hi</html>"
    page.close()
    # page.close() 不得误杀共享 session
    assert browser.session_id == "fake-session"
    assert _stops(fake_bsk) == []


def test_goto_passes_wait_until_and_ignores_ms_timeout(fake_bsk):
    browser, _ = fake_bsk.build()
    browser.start()
    browser.page().goto("https://example.com", wait_until="domcontentloaded", timeout=30000)
    nav = _subcommand_calls(fake_bsk, "navigate")[-1]
    assert nav["argv"][1] == "https://example.com"
    assert "--wait-until" in nav["argv"]
    assert nav["argv"][nav["argv"].index("--wait-until") + 1] == "domcontentloaded"
    assert "--session" in nav["argv"]


def test_goto_retries_with_context_when_daemon_requires_it(fake_bsk):
    """共享会话里第二次导航必须带 cooperate sync 的上下文，否则整批只有第一个源能抓。"""
    browser, _ = fake_bsk.build(navigate_requires_context=True)
    browser.start()
    browser.page().goto("https://example.com/a")
    browser.page().goto("https://example.com/b")
    syncs = _subcommand_calls(fake_bsk, "cooperate")
    navs = _subcommand_calls(fake_bsk, "navigate")
    assert len(navs) == 4  # 两个源各一次无上下文尝试 + 一次带上下文重试
    assert "--context" not in navs[0]["argv"]
    assert navs[1]["argv"][navs[1]["argv"].index("--context") + 1] == "ctx-fake"
    assert len(syncs) == 2


def test_navigate_failure_other_than_missing_context_is_not_retried(fake_bsk):
    browser, _ = fake_bsk.build(
        navigate_exit=1,
        navigate_stdout='{"code": "not_found", "message": "session not registered", "exit_code": 1}',
    )
    browser.start()
    with pytest.raises(BskCommandError) as excinfo:
        browser.page().goto("https://example.com")
    assert "not_found" in str(excinfo.value)
    assert _subcommand_calls(fake_bsk, "cooperate") == []


def test_acquire_page_by_url_navigates_when_current_url_unreadable():
    """新会话 tab 上内容脚本未注入时读不到 URL，必须直接导航（首动作允许无上下文）。"""
    from connectors._shared.bsk_session import BskRunnerSession

    class FakePage:
        gotos: list[str] = []

        @property
        def url(self):
            raise BskCommandError("cooperate sync 失败: Could not establish connection")

        def goto(self, url, wait_until=None):
            self.gotos.append(url)

    page = FakePage()

    class FakeBrowser:
        def page(self):
            return page

    session = BskRunnerSession()
    session._browser = FakeBrowser()
    assert session.acquire_page_by_url("https://example.com/x") is page
    assert page.gotos == ["https://example.com/x"]


def test_log_lines_carry_label_url_exit_and_duration(fake_bsk):
    browser, logs = fake_bsk.build()
    browser.start()
    browser.page().goto("https://example.com")
    assert any(line.startswith("cmd=navigate url=https://example.com exit=0 dur=") for line in logs)
    assert any(line.startswith("cmd=session start exit=0 dur=") for line in logs)


def test_no_credentials_in_logs_or_errors(fake_bsk):
    secret = "SUPER-SECRET-TOKEN-42"
    browser, logs = fake_bsk.build(
        stderr_secret=secret,
        evaluate_exit=1,
        evaluate_default={"code": "internal", "message": f"failed with {secret}"},
    )
    browser.start()
    with pytest.raises(BskCommandError) as excinfo:
        browser.evaluate("1+1")
    blob = "\n".join(logs) + "\n" + str(excinfo.value)
    assert secret not in blob


# ---- 超时后收尾：stop 重试（M1） ----


def test_timeout_stop_failure_is_retried_once(fake_bsk, monkeypatch):
    """超时后 session 仍 busy，首次 stop 被 daemon 拒绝（exit=4）→ 延迟重试一次即成功。"""
    monkeypatch.setattr(bsk_browser, "STOP_RETRY_DELAY_SECONDS", 0)
    browser, _ = fake_bsk.build(
        evaluate_sleep=5, timeout=0.3, request_help_timeout=None, stop_exit_seq=[4, 0]
    )
    browser.start()
    with pytest.raises(BskTimeout):
        browser.evaluate("1+1")
    stops = _stops(fake_bsk)
    assert [c["argv"][2] for c in stops] == ["fake-session", "fake-session"]
    assert browser.session_id is None


def test_stop_retry_failure_is_only_logged(fake_bsk, monkeypatch):
    monkeypatch.setattr(bsk_browser, "STOP_RETRY_DELAY_SECONDS", 0)
    browser, logs = fake_bsk.build(stop_exit=4)
    browser.start()
    browser.stop()  # 重试仍失败也不得抛
    assert len(_stops(fake_bsk)) == 2
    assert any("err=retry-failed" in line for line in logs)


def test_stop_is_not_killed_by_request_help_watchdog(fake_bsk):
    """stop 是收尾命令，与登录阻塞无关：极小 request_help_timeout 不得把它提前 kill。"""
    browser, logs = fake_bsk.build(stop_sleep=0.5, timeout=5.0, request_help_timeout=0.1)
    browser.start()
    browser.stop()
    assert len(_stops(fake_bsk)) == 1
    assert not any("err=BskHelpTimeout" in line for line in logs)


# ---- start 超时：孤儿 session 清理 + 分类（M2） ----


def test_start_timeout_is_plain_timeout_not_help_timeout(fake_bsk):
    browser, logs = fake_bsk.build(start_sleep=5, timeout=0.3, request_help_timeout=0.3)
    with pytest.raises(BskTimeout) as excinfo:
        browser.start()
    # start 阶段尚未打开任何页面，不得被误分类成「疑似登录/验证码阻塞」
    assert not isinstance(excinfo.value, BskHelpTimeout)
    assert any("err=BskTimeout" in line for line in logs)


def test_start_timeout_cleans_up_orphan_session(fake_bsk):
    started = int(time.time() * 1000)
    browser, _ = fake_bsk.build(
        start_sleep=5,
        timeout=0.3,
        request_help_timeout=0.3,
        list_sessions=[
            {"session_id": "orphan-new", "created_at_ms": started + 60_000},
            {"session_id": "not-ours-old", "created_at_ms": started - 600_000},
        ],
    )
    with pytest.raises(BskTimeout):
        browser.start()
    assert browser.session_id is None
    stopped = [c["argv"][2] for c in _stops(fake_bsk)]
    assert stopped == ["orphan-new"]  # 只清本次新建的孤儿，不动别人的 session
    assert _subcommand_calls(fake_bsk, "session")[0]["argv"][0:3] == ["session", "start", "--json"]
    assert any(c["argv"][0:3] == ["session", "list", "--json"] for c in fake_bsk.calls())


def test_start_failure_does_not_run_orphan_cleanup(fake_bsk):
    """非超时的 start 失败走既有路径（解析到 id 才补 stop），不该额外 list。"""
    browser, _ = fake_bsk.build(start_exit=1, start_stdout="error: no browser connected")
    with pytest.raises(BskSessionError):
        browser.start()
    assert not any(c["argv"][0:3] == ["session", "list", "--json"] for c in fake_bsk.calls())

# ---- P1: Playwright 同形方法（evaluate / evaluate_all / wait / 滚轮） ----


def test_new_method_timeout_defaults_exceed_watchdog():
    """新方法的默认预算必须明显高于 30s 看门狗门限，避免被误分类成 BskHelpTimeout。"""
    assert bsk_browser.DEFAULT_EVALUATE_TIMEOUT_SECONDS > bsk_browser.DEFAULT_REQUEST_HELP_TIMEOUT_SECONDS
    assert bsk_browser.DEFAULT_WAIT_TIMEOUT_MS / 1000.0 > bsk_browser.DEFAULT_REQUEST_HELP_TIMEOUT_SECONDS


def test_page_evaluate_without_arg_wraps_expression_but_keeps_value(fake_bsk):
    """bsk evaluate 走 CDP Runtime.evaluate，不会自动调用函数表达式，故无参路径必须包装。"""
    browser, _ = fake_bsk.build(evaluate={"document.title": {"ok": True, "value": "标题"}})
    browser.start()
    assert browser.page().evaluate("document.title") == "标题"
    expr = fake_bsk.evaluate_exprs()[-1]
    assert "document.title" in expr  # 表达式原样嵌入
    assert "typeof f==='function'?f():f" in expr  # 是函数才调用，普通表达式原样返回
    # --await-promise 保持默认 true（不显式关掉），供 await fetch(...) 使用
    assert "--await-promise" not in _subcommand_calls(fake_bsk, "evaluate")[-1]["argv"]


def test_page_evaluate_invokes_bare_function_expression(fake_bsk):
    """回归钉：裸 `() => {...}` 在 bsk 下会序列化成 {}（真机 0 条根因），必须被调用。"""
    browser, _ = fake_bsk.build(evaluate={"() =>": {"ok": True, "value": [{"id": "1"}]}})
    browser.start()
    assert browser.page().evaluate("() => [{id: '1'}]") == [{"id": "1"}]
    assert fake_bsk.evaluate_exprs()[-1] == "((f)=>typeof f==='function'?f():f)((() => [{id: '1'}]))"


def test_page_evaluate_with_arg_serializes_non_ascii_safely(fake_bsk):
    arg = {"标题": "甲", "n": 1}
    browser, _ = fake_bsk.build(evaluate_default={"ok": True, "value": 42})
    browser.start()
    assert browser.page().evaluate("(a) => a.n", arg) == 42
    expr = fake_bsk.evaluate_exprs()[-1]
    # 传参约定：函数表达式被 (js)(<json arg>) 调用；arg 由 json.dumps 默认转义非 ASCII
    assert expr == f"((a) => a.n)({json.dumps(arg)})"
    assert r"\u6807\u9898" in expr
    assert "标题" not in expr  # 非 ASCII 已转义，规避 Windows 命令行编码


def test_page_evaluate_async_fetch_keeps_await_promise(fake_bsk):
    browser, _ = fake_bsk.build(evaluate={"fetch(": {"ok": True, "value": 200}})
    browser.start()
    assert browser.page().evaluate("fetch(location.href).then(r=>r.status)") == 200
    argv = _subcommand_calls(fake_bsk, "evaluate")[-1]["argv"]
    assert "--await-promise" not in argv  # 未显式传 false，即保持默认 true


def test_page_evaluate_default_timeout_is_60s_and_watchdog_off(fake_bsk):
    browser, logs = fake_bsk.build(evaluate_sleep=0.3, timeout=30.0, request_help_timeout=0.1)
    browser.start()
    # request_help_timeout 极小也不得把慢 evaluate 误判成 BskHelpTimeout
    assert browser.page().evaluate("1+1") is None
    assert not any("err=BskHelpTimeout" in line for line in logs)
    argv = _subcommand_calls(fake_bsk, "evaluate")[-1]["argv"]
    assert argv[argv.index("--timeout") + 1] == "60s"


def test_page_evaluate_accepts_larger_timeout(fake_bsk):
    browser, _ = fake_bsk.build()
    browser.start()
    browser.page().evaluate("1+1", timeout=120)
    argv = _subcommand_calls(fake_bsk, "evaluate")[-1]["argv"]
    assert argv[argv.index("--timeout") + 1] == "120s"


def test_locator_evaluate_all_passes_element_array_and_returns_list(fake_bsk):
    browser, _ = fake_bsk.build(evaluate_default={"ok": True, "value": [{"id": "1"}, {"id": "2"}]})
    browser.start()
    items = browser.page().locator(".item").evaluate_all("els => els.map(e => ({id: e.id}))")
    assert items == [{"id": "1"}, {"id": "2"}]
    expr = fake_bsk.evaluate_exprs()[-1]
    # Playwright 语义：第一入参是「全部命中元素数组」，非单元素
    assert "els.map(e => ({id: e.id}))" in expr
    assert "querySelectorAll" in expr
    assert json.dumps(None) in expr  # 未传 arg → null 占位
    assert len(fake_bsk.evaluate_exprs()) == 1  # 单次往返


def test_locator_evaluate_all_with_arg_and_no_match_returns_empty(fake_bsk):
    browser, _ = fake_bsk.build(evaluate_default={"ok": True, "value": []})
    browser.start()
    assert browser.page().locator(".nope").evaluate_all("(els, limit) => els.slice(0, limit)", 5) == []
    expr = fake_bsk.evaluate_exprs()[-1]
    assert json.dumps(5) in expr
    assert json.dumps(".nope") in expr


def test_wait_for_timeout_uses_wait_ms_and_clamps_invalid(fake_bsk):
    browser, _ = fake_bsk.build()
    browser.start()
    page = browser.page()
    page.wait_for_timeout(1500)
    calls = _subcommand_calls(fake_bsk, "wait-ms")
    assert calls and calls[-1]["argv"][1] == "1500ms"
    assert "--session" not in calls[-1]["argv"]  # wait-ms 不需要 session
    before = len(calls)
    page.wait_for_timeout(-5)
    page.wait_for_timeout("abc")
    assert len(_subcommand_calls(fake_bsk, "wait-ms")) == before  # 非法值不产生命令


def test_wait_for_selector_found_returns_with_polling_budget(fake_bsk):
    browser, _ = fake_bsk.build(evaluate_default={"ok": True, "value": True})
    browser.start()
    browser.page().wait_for_selector(".ready", timeout=2000)
    expr = fake_bsk.evaluate_exprs()[-1]
    assert "new Promise" in expr and "querySelectorAll" in expr and ".ready" in expr
    argv = _subcommand_calls(fake_bsk, "evaluate")[-1]["argv"]
    assert argv[argv.index("--timeout") + 1] == "12s"  # 页内 2s + 10s 余量


def test_wait_for_selector_timeout_raises_command_error(fake_bsk):
    browser, _ = fake_bsk.build(
        evaluate_default={"ok": False, "error": {"text": "等待选择器超时（2000ms 内未满足 visible）"}}
    )
    browser.start()
    with pytest.raises(BskCommandError) as excinfo:
        browser.page().wait_for_selector(".nope", timeout=2000)
    assert "等待选择器超时" in str(excinfo.value)


def test_locator_wait_for_routes_through_page_wait(fake_bsk):
    browser, _ = fake_bsk.build(evaluate_default={"ok": True, "value": True})
    browser.start()
    browser.page().locator(".card").first.wait_for(timeout=1000)
    expr = fake_bsk.evaluate_exprs()[-1]
    assert json.dumps(".card") in expr
    assert "new Promise" in expr and "querySelectorAll" in expr  # 复用页内轮询实现


def test_wait_for_state_visible_attached_and_fallback(fake_bsk):
    browser, _ = fake_bsk.build(evaluate_default={"ok": True, "value": True})
    browser.start()
    page = browser.page()
    page.wait_for_selector(".a", timeout=500, state="attached")
    assert "const attachedOnly=true" in fake_bsk.evaluate_exprs()[-1]
    page.wait_for_selector(".b", timeout=500)  # 默认 visible
    assert "const attachedOnly=false" in fake_bsk.evaluate_exprs()[-1]
    page.wait_for_selector(".c", timeout=500, state="hidden")  # 未知态降级为 attached
    assert "const attachedOnly=true" in fake_bsk.evaluate_exprs()[-1]


def test_mouse_wheel_sends_both_deltas(fake_bsk):
    browser, _ = fake_bsk.build()
    browser.start()
    browser.page().mouse.wheel(0, 2600)
    argv = _subcommand_calls(fake_bsk, "wheel")[-1]["argv"]
    assert argv[argv.index("--delta-y") + 1] == "2600"
    assert argv[argv.index("--delta-x") + 1] == "0"
    assert "--session" in argv
    browser.page().mouse.wheel(30, -40)  # 负 delta 表示反向滚动，不得被钳成 0
    argv = _subcommand_calls(fake_bsk, "wheel")[-1]["argv"]
    assert argv[argv.index("--delta-x") + 1] == "30"
    assert argv[argv.index("--delta-y") + 1] == "-40"


def test_mouse_wheel_both_zero_is_noop(fake_bsk):
    browser, _ = fake_bsk.build()
    browser.start()
    browser.page().mouse.wheel(0, 0)
    assert _subcommand_calls(fake_bsk, "wheel") == []
