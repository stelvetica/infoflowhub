from __future__ import annotations

import json
from pathlib import Path

from connectors._shared import chrome_runner


def _make_profile(tmp_path: Path) -> Path:
    profile = tmp_path / "auth-profile"
    network = profile / "Network"
    network.mkdir(parents=True)
    (profile / "Preferences").write_text("{}", encoding="utf-8")
    (network / "Cookies").write_bytes(b"cookies")
    return profile


def test_shared_runner_session_uses_launch_persistent_context(monkeypatch, tmp_path):
    """SharedRunnerSession.start() 用 launch_persistent_context 挂载传入的 profile 目录。"""
    profile = _make_profile(tmp_path)

    monkeypatch.setattr(chrome_runner, "kill_chrome_gracefully", lambda: True)
    monkeypatch.setattr(chrome_runner, "_ensure_all_chrome_processes_stopped", lambda timeout_seconds=20: True)
    monkeypatch.setattr(chrome_runner, "_force_kill_chrome_powershell", lambda: True)
    monkeypatch.setattr(chrome_runner, "_resolve_default_browser_executable", lambda: ("chrome", "chrome.exe"))

    captured = {}

    class FakeContext:
        pages = []
        def new_page(self):
            return object()
        def close(self):
            captured.setdefault("context_closed", []).append(True)

    class FakeChromium:
        def launch_persistent_context(self, *, user_data_dir, executable_path, headless, args, ignore_default_args):
            captured["user_data_dir"] = user_data_dir
            captured["executable_path"] = executable_path
            captured["headless"] = headless
            return FakeContext()

    class FakePw:
        chromium = FakeChromium()
        def start(self):
            return self
        def stop(self):
            captured.setdefault("pw_stopped", []).append(True)

    import sys, types
    fake_mod = types.ModuleType("playwright.sync_api")
    fake_mod.sync_playwright = lambda: FakePw()
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_mod)

    session = chrome_runner.SharedRunnerSession(source_profile_dir=profile, extra_args=["--user-agent=UA"])
    session.start()
    assert str(profile) in captured["user_data_dir"] or captured["user_data_dir"] == str(profile)
    assert captured["headless"] is True
    session.shutdown()
    assert "context_closed" in captured
    assert "pw_stopped" in captured


def test_shared_runner_session_restart_restarts_context(monkeypatch, tmp_path):
    """restart() 关闭后重新启动 context。"""
    profile = _make_profile(tmp_path)

    monkeypatch.setattr(chrome_runner, "kill_chrome_gracefully", lambda: True)
    monkeypatch.setattr(chrome_runner, "_ensure_all_chrome_processes_stopped", lambda timeout_seconds=20: True)
    monkeypatch.setattr(chrome_runner, "_force_kill_chrome_powershell", lambda: True)
    monkeypatch.setattr(chrome_runner, "_resolve_default_browser_executable", lambda: ("chrome", "chrome.exe"))

    launches = []

    class FakeContext:
        pages = []
        def new_page(self):
            return object()
        def close(self):
            pass

    class FakeChromium:
        def launch_persistent_context(self, **kwargs):
            launches.append(kwargs)
            return FakeContext()

    class FakePw:
        chromium = FakeChromium()
        def start(self):
            return self
        def stop(self):
            pass

    import sys, types
    fake_mod = types.ModuleType("playwright.sync_api")
    fake_mod.sync_playwright = lambda: FakePw()
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_mod)

    session = chrome_runner.SharedRunnerSession(source_profile_dir=profile)
    session.start()
    session.restart()
    session.shutdown()
    assert len(launches) == 2  # start + restart


def test_shared_runner_session_acquire_page_requires_start(monkeypatch, tmp_path):
    """未启动时 acquire_page 抛错。"""
    session = chrome_runner.SharedRunnerSession(source_profile_dir=_make_profile(tmp_path))
    try:
        session.acquire_page()
        assert False, "should have raised"
    except RuntimeError:
        pass


def test_strip_google_cookies_removes_google_only(tmp_path):
    """副本剥离：Google 系域 cookie 删净，其他域保留。"""
    import sqlite3

    cookies_path = tmp_path / "Network" / "Cookies"
    cookies_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(cookies_path))
    conn.execute("CREATE TABLE cookies (host_key TEXT, name TEXT)")
    rows = [
        (".google.com", "SID"),
        ("accounts.google.com", "HSID"),
        (".youtube.com", "VISITOR_INFO1_LIVE"),
        ("mp.weixin.qq.com", "sessionid"),
        (".sspai.com", "token"),
    ]
    conn.executemany("INSERT INTO cookies VALUES (?, ?)", rows)
    conn.commit()
    conn.close()

    chrome_runner._strip_google_cookies(tmp_path)

    conn = sqlite3.connect(str(cookies_path))
    left = conn.execute("SELECT host_key FROM cookies").fetchall()
    conn.close()
    assert sorted(h for (h,) in left) == [".sspai.com", "mp.weixin.qq.com"]


def test_strip_google_cookies_absent_file_no_error(tmp_path):
    """Network/Cookies 不存在时不报错。"""
    chrome_runner._strip_google_cookies(tmp_path)


def test_strip_google_identity_cleans_secure_preferences(tmp_path):
    """Secure Preferences 中的 signin/google.services 必须连 MAC 一起剥掉，
    否则 Chrome 运行时恢复账号状态并以已登录身份向 Google 发校验请求。"""
    runner_dir = tmp_path / "runner"
    profile = runner_dir / "Default"
    profile.mkdir(parents=True)
    (runner_dir / "Local State").write_text(
        '{"account_info": [{"email": "a@x.com"}], "os_crypt": {"key": "k"}}', encoding="utf-8"
    )
    secure = {
        "google.services": {"account_id": "111704574367954466831"},
        "signin": {"allowed": True},
        "protection": {"macs": {"google.services": {"account_id": "MAC1"}, "signin": {"allowed": "MAC2"}, "other": {"x": "KEEP"}}},
    }
    (profile / "Secure Preferences").write_text(json.dumps(secure), encoding="utf-8")
    (profile / "Preferences").write_text(json.dumps({"signin": {"allowed": True}, "download": {"x": 1}}), encoding="utf-8")

    chrome_runner._strip_google_identity(runner_dir, profile)

    secure_now = json.loads((profile / "Secure Preferences").read_text(encoding="utf-8"))
    assert "google.services" not in secure_now
    assert "signin" not in secure_now
    assert secure_now["protection"]["macs"] == {"other": {"x": "KEEP"}}
    prefs_now = json.loads((profile / "Preferences").read_text(encoding="utf-8"))
    assert "signin" not in prefs_now
    assert prefs_now["download"] == {"x": 1}
    ls_now = json.loads((runner_dir / "Local State").read_text(encoding="utf-8"))
    assert "account_info" not in ls_now
    assert ls_now["os_crypt"] == {"key": "k"}


def test_rebuild_clock_shared_across_runners(monkeypatch, tmp_path):
    """统一计时：任一副本重建刷新全局时钟，全体副本在间隔内不再重建。"""
    clock = tmp_path / ".rebuild_clock.json"
    monkeypatch.setattr(chrome_runner, "REBUILD_CLOCK_PATH", clock)

    assert chrome_runner._read_rebuild_clock() == 0
    chrome_runner._write_rebuild_clock()
    assert chrome_runner._read_rebuild_clock() > 0

    ready = tmp_path / "p"
    ready.mkdir()
    (ready / "Preferences").write_text("{}", encoding="utf-8")
    cookies = ready / "Network" / "Cookies"
    cookies.parent.mkdir()
    cookies.write_bytes(b"x")

    runner_dir = tmp_path / "runner-a"
    runner_dir.mkdir()
    assert not chrome_runner.should_rebuild_runner_profile(ready, runner_dir)

    # 时钟过期 → 需要重建
    import time as _t
    old = {"rebuilt_at": int(_t.time()) - 8 * 24 * 3600}
    clock.write_text(json.dumps(old), encoding="utf-8")
    assert chrome_runner.should_rebuild_runner_profile(ready, runner_dir)
