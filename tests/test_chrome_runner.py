from __future__ import annotations

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
