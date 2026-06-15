from __future__ import annotations

from connectors.alphapai import browser as alphapai_browser
from connectors.alphapai import runner as alphapai_runner
from connectors.rss import fetch as rss_fetch
from apps.subscriptions.models import FeedFetchResult


def test_ensure_alphapai_browser_prepares_profile_before_rebuild(monkeypatch, tmp_path):
    runner_dir = tmp_path / "runner"
    profile_dir = runner_dir / "Default"
    source_dir = tmp_path / "chrome-user-data"

    monkeypatch.setattr(alphapai_browser, "ALPHAPAI_RUNNER_DIR", runner_dir)
    monkeypatch.setattr(alphapai_browser, "ALPHAPAI_RUNNER_PROFILE_DIR", profile_dir)
    monkeypatch.setattr(alphapai_browser, "ALPHAPAI_RUNNER_META_PATH", runner_dir / ".meta.json")
    monkeypatch.setattr(alphapai_browser, "CHROME_USER_DATA", source_dir)
    monkeypatch.setattr(alphapai_browser, "ROOT_FILES_TO_COPY", ())
    monkeypatch.setattr(alphapai_browser, "PROFILE_FILES_TO_COPY", ("Preferences",))
    monkeypatch.setattr(alphapai_browser, "PROFILE_DIRS_TO_COPY", ("Network",))
    monkeypatch.setattr(alphapai_browser, "is_alphapai_debug_browser_ready", lambda: False)
    monkeypatch.setattr(alphapai_browser, "wait_for_alphapai_debug_browser", lambda: True)

    rebuild_called = False

    def fail_rebuild() -> bool:
        nonlocal rebuild_called
        rebuild_called = True
        return False

    monkeypatch.setattr(alphapai_browser, "try_rebuild_alphapai_runner_profile", fail_rebuild)
    monkeypatch.setattr(alphapai_browser, "_resolve_default_browser_executable", lambda: ("chrome", "chrome.exe"))
    monkeypatch.setattr(alphapai_browser.subprocess, "Popen", lambda *args, **kwargs: object())

    source_default = source_dir / "Default"
    source_network = source_default / "Network"
    source_network.mkdir(parents=True)
    (source_default / "Preferences").write_text("{}", encoding="utf-8")
    (source_network / "Cookies").write_bytes(b"cookie-db")

    alphapai_browser.ensure_alphapai_debug_browser()

    assert not rebuild_called
    assert (profile_dir / "Preferences").exists()
    assert (profile_dir / "Network" / "Cookies").read_bytes() == b"cookie-db"


def test_prepare_profile_tolerates_locked_noncritical_files(monkeypatch, tmp_path):
    runner_dir = tmp_path / "runner"
    profile_dir = runner_dir / "Default"
    source_dir = tmp_path / "chrome-user-data"

    monkeypatch.setattr(alphapai_browser, "ALPHAPAI_RUNNER_DIR", runner_dir)
    monkeypatch.setattr(alphapai_browser, "ALPHAPAI_RUNNER_PROFILE_DIR", profile_dir)
    monkeypatch.setattr(alphapai_browser, "ALPHAPAI_RUNNER_META_PATH", runner_dir / ".meta.json")
    monkeypatch.setattr(alphapai_browser, "CHROME_USER_DATA", source_dir)
    monkeypatch.setattr(alphapai_browser, "ROOT_FILES_TO_COPY", ())
    monkeypatch.setattr(alphapai_browser, "PROFILE_FILES_TO_COPY", ("Preferences", "History"))
    monkeypatch.setattr(alphapai_browser, "PROFILE_DIRS_TO_COPY", ("Network",))

    source_default = source_dir / "Default"
    source_network = source_default / "Network"
    source_network.mkdir(parents=True)
    (source_default / "Preferences").write_text("{}", encoding="utf-8")
    (source_network / "Cookies").write_bytes(b"cookie-db")

    real_copy_sqlite = alphapai_browser._copy_sqlite_best_effort

    def copy_sqlite_with_locked_history(src, dst):
        if src.name == "History":
            return False
        return real_copy_sqlite(src, dst)

    monkeypatch.setattr(alphapai_browser, "_copy_sqlite_best_effort", copy_sqlite_with_locked_history)

    alphapai_browser.prepare_alphapai_runner_profile()

    assert (profile_dir / "Preferences").exists()
    assert not (profile_dir / "History").exists()
    assert (profile_dir / "Network" / "Cookies").read_bytes() == b"cookie-db"


def test_run_fetch_once_closes_dedicated_browser(monkeypatch):
    closed = []

    class FakePage:
        url = ""

        def goto(self, *args, **kwargs):
            return None

    class FakeContext:
        pages = []

        def new_page(self):
            return FakePage()

    class FakeBrowser:
        contexts = [FakeContext()]

        def close(self):
            closed.append("browser")

    class FakeChromium:
        def connect_over_cdp(self, endpoint):
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(alphapai_runner, "sync_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(alphapai_runner, "connect_over_cdp_endpoint", lambda: "http://127.0.0.1:9222")
    monkeypatch.setattr(alphapai_runner, "find_alphapai_tab_url", lambda: "https://alphapai-web.rabyte.cn/reading/home/market-report/detail")
    monkeypatch.setattr(alphapai_runner, "close_alphapai_debug_browser", lambda: closed.append("debug"))
    monkeypatch.setattr(
        alphapai_runner,
        "fetch_alphapai_with_page",
        lambda page, source, timeout_ms, limit: FeedFetchResult(
            source_id=source["id"],
            source_name=source["name"],
            feed_url=source["feed_url"],
            ok=True,
            status=200,
            entries=[],
        ),
    )

    result = alphapai_runner._run_fetch_once(
        {"id": "alphapai", "name": "Alpha", "feed_url": "https://example.com"},
        limit=10,
        timeout_ms=1000,
    )

    assert result.ok
    assert closed == ["browser", "debug"]


def test_fetch_many_uses_alphapai_limit(monkeypatch):
    seen_limits = []

    def fake_fetch_alphapai_source(source, *, limit):
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
