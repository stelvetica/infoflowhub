from __future__ import annotations

from connectors._shared import chrome_runner
from connectors.alphapai import browser as alphapai_browser
from connectors.alphapai import runner as alphapai_runner
from connectors.rss import fetch as rss_fetch
from apps.subscriptions.models import FeedFetchResult


def test_ensure_debug_browser_prepares_profile_when_missing(monkeypatch, tmp_path):
    """ensure_debug_browser 在 profile 缺失时复制 profile 并启动浏览器。"""
    runner_dir = tmp_path / "shared-runner"
    source_dir = tmp_path / "chrome-user-data"
    source_default = source_dir / "Default"
    source_network = source_default / "Network"
    source_network.mkdir(parents=True)
    (source_default / "Preferences").write_text("{}", encoding="utf-8")
    (source_network / "Cookies").write_bytes(b"cookie-db")

    monkeypatch.setattr(chrome_runner, "CHROME_USER_DATA", source_dir)
    monkeypatch.setattr(chrome_runner, "is_debug_browser_ready", lambda port: False)
    monkeypatch.setattr(chrome_runner, "wait_for_debug_browser", lambda port, timeout_seconds=20: True)
    monkeypatch.setattr(chrome_runner, "_stop_chrome_for_profile_copy", lambda: None)
    monkeypatch.setattr(chrome_runner, "_resolve_default_browser_executable", lambda: ("chrome", "chrome.exe"))
    monkeypatch.setattr(chrome_runner.subprocess, "Popen", lambda *args, **kwargs: object())

    chrome_runner.ensure_debug_browser(
        runner_dir, "Default", 9280, "about:blank",
        source_profile_dir=source_default,
        root_files=(), profile_files=("Preferences",), profile_dirs=("Network",),
    )

    profile = runner_dir / "Default"
    assert (profile / "Preferences").exists()
    assert (profile / "Network" / "Cookies").read_bytes() == b"cookie-db"


def test_prepare_runner_profile_tolerates_locked_noncritical_files(monkeypatch, tmp_path):
    """SQLite 文件被锁时（如 History），复制不中断，跳过该文件。"""
    runner_dir = tmp_path / "runner"
    source = tmp_path / "source" / "Default"
    network = source / "Network"
    network.mkdir(parents=True)
    (source / "Preferences").write_text("{}", encoding="utf-8")
    (source / "History").write_bytes(b"history")
    (network / "Cookies").write_bytes(b"cookies")

    real_copy = chrome_runner._copy_sqlite_best_effort

    def locked_history(src, dst):
        if src.name == "History":
            return False
        return real_copy(src, dst)

    monkeypatch.setattr(chrome_runner, "_copy_sqlite_best_effort", locked_history)

    chrome_runner.prepare_runner_profile(
        source, runner_dir, "Default",
        root_files=(), profile_files=("Preferences", "History"), profile_dirs=("Network",),
    )

    profile = runner_dir / "Default"
    assert (profile / "Preferences").exists()
    assert not (profile / "History").exists()
    assert (profile / "Network" / "Cookies").read_bytes() == b"cookies"


def test_ensure_debug_browser_reuses_live_browser(monkeypatch, tmp_path):
    """debug browser 已就绪且 profile 未过期时，不重新复制。"""
    runner_dir = tmp_path / "runner"
    monkeypatch.setattr(chrome_runner, "is_debug_browser_ready", lambda port: True)
    monkeypatch.setattr(chrome_runner, "should_rebuild_runner_profile", lambda *args, **kwargs: False)

    copy_called = []

    def no_copy(*args, **kwargs):
        copy_called.append(True)
        raise AssertionError("should not copy when browser ready and profile fresh")

    monkeypatch.setattr(chrome_runner, "prepare_runner_profile", no_copy)
    monkeypatch.setattr(chrome_runner, "_resolve_default_browser_executable", lambda: ("chrome", "chrome.exe"))
    monkeypatch.setattr(chrome_runner.subprocess, "Popen", lambda *args, **kwargs: object())
    monkeypatch.setattr(chrome_runner, "wait_for_debug_browser", lambda port, timeout_seconds=20: True)

    chrome_runner.ensure_debug_browser(
        runner_dir, "Default", 9280, "about:blank",
        source_profile_dir=tmp_path / "source",
    )
    assert not copy_called


def test_shared_runner_session_restart_reuses_runner_dir(monkeypatch, tmp_path):
    """SharedRunnerSession.restart() 关闭后重启，复用同一 runner 目录与端口。"""
    runner_dir = tmp_path / "shared"
    source = tmp_path / "source" / "Default"
    network = source / "Network"
    network.mkdir(parents=True)
    (source / "Preferences").write_text("{}", encoding="utf-8")
    (network / "Cookies").write_bytes(b"c")

    starts = []
    monkeypatch.setattr(chrome_runner, "is_debug_browser_ready", lambda port: False)
    monkeypatch.setattr(chrome_runner, "wait_for_debug_browser", lambda port, timeout_seconds=20: True)
    monkeypatch.setattr(chrome_runner, "_stop_chrome_for_profile_copy", lambda: None)
    monkeypatch.setattr(chrome_runner, "_resolve_default_browser_executable", lambda: ("chrome", "chrome.exe"))
    monkeypatch.setattr(chrome_runner, "close_debug_browser", lambda runner_dir, debug_port: None)

    class _FakeProc:
        def __init__(self, args):
            starts.append(args)
    monkeypatch.setattr(chrome_runner.subprocess, "Popen", lambda *args, **kwargs: _FakeProc(args))

    # 不真正连 CDP，把 playwright 连接 mock 掉
    class FakeBrowser:
        contexts = []
        def close(self): pass
    class FakePw:
        chromium = type("C", (), {"connect_over_cdp": staticmethod(lambda endpoint: FakeBrowser())})
        def start(self): return self
        def stop(self): pass

    import sys, types
    fake_mod = types.ModuleType("playwright.sync_api")
    fake_mod.sync_playwright = lambda: FakePw()
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_mod)

    session = chrome_runner.SharedRunnerSession(
        source_profile_dir=source,
        runner_dir=runner_dir,
        debug_port=9280,
        extra_args=[],
    )
    session.start()
    session.restart()
    session.shutdown()
    assert len(starts) == 2  # start + restart 各启动一次 Chrome


def test_fetch_many_uses_alphapai_limit(monkeypatch):
    """fetch_many 对 alphapai 源传入 FETCH_SOURCE_LIMIT。"""
    seen_limits = []

    def fake_fetch_alphapai_source(source, *, limit, session=None):
        seen_limits.append(limit)
        return FeedFetchResult(
            source_id=source["id"],
            source_name=source["name"],
            feed_url=source["feed_url"],
            ok=True,
            status=200,
            entries=[],
        )

    monkeypatch.setattr(rss_fetch, "fetch_alphapai_source", fake_fetch_alphapai_source)

    rss_fetch.fetch_many(
        [
            {
                "id": "alphapai",
                "name": "Alpha",
                "feed_url": "https://alphapai-web.rabyte.cn/reading/home/market-report/detail",
                "provider": "web",
            }
        ]
    )

    assert seen_limits == [60]
