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
    sys.stdout.write(scenario.get("navigate_stdout", "tab=1 reached=load\\nurl=" + argv[1]))
    sys.exit(scenario.get("navigate_exit", 0))

if cmd == "get-html":
    sys.stdout.write(scenario.get("html", "<html></html>"))
    sys.exit(scenario.get("html_exit", 0))

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
    assert fake_bsk.calls()[0]["argv"][0:3] == ["session", "start", "--json"]


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