from __future__ import annotations

from pathlib import Path

from connectors._shared import chrome_runner


def _make_source_profile(tmp_path: Path) -> Path:
    source = tmp_path / "source" / "Default"
    network = source / "Network"
    network.mkdir(parents=True)
    (source / "Preferences").write_text("{}", encoding="utf-8")
    (network / "Cookies").write_bytes(b"cookies")
    return source


def test_prepare_runner_profile_copies_cookies_and_preferences(tmp_path):
    source = _make_source_profile(tmp_path)
    runner_dir = tmp_path / "runner"

    chrome_runner.prepare_runner_profile(
        source,
        runner_dir,
        "Default",
        root_files=(),
        profile_files=("Preferences",),
        profile_dirs=("Network",),
    )

    profile = runner_dir / "Default"
    assert (profile / "Preferences").exists()
    assert (profile / "Network" / "Cookies").read_bytes() == b"cookies"


def test_prepare_runner_profile_tolerates_locked_history(tmp_path, monkeypatch):
    source = _make_source_profile(tmp_path)
    (source / "History").write_bytes(b"history")
    runner_dir = tmp_path / "runner"

    real_copy = chrome_runner._copy_sqlite_best_effort

    def locked_history(src, dst):
        if src.name == "History":
            return False
        return real_copy(src, dst)

    monkeypatch.setattr(chrome_runner, "_copy_sqlite_best_effort", locked_history)

    chrome_runner.prepare_runner_profile(
        source,
        runner_dir,
        "Default",
        root_files=(),
        profile_files=("Preferences", "History"),
        profile_dirs=("Network",),
    )

    profile = runner_dir / "Default"
    assert (profile / "Preferences").exists()
    assert not (profile / "History").exists()
    assert (profile / "Network" / "Cookies").read_bytes() == b"cookies"


def test_should_rebuild_when_profile_missing(tmp_path):
    runner_dir = tmp_path / "runner"
    profile_dir = runner_dir / "Default"
    assert chrome_runner.should_rebuild_runner_profile(profile_dir, runner_dir, 3600)


def test_should_rebuild_when_meta_expired(tmp_path):
    source = _make_source_profile(tmp_path)
    runner_dir = tmp_path / "runner"
    chrome_runner.prepare_runner_profile(
        source, runner_dir, "Default", root_files=(), profile_files=("Preferences",), profile_dirs=("Network",)
    )
    chrome_runner._write_runner_meta(
        runner_dir,
        profile_name="Default",
        source_profile_dir=source,
        rebuilt_at=0,
    )

    assert chrome_runner.should_rebuild_runner_profile(
        runner_dir / "Default", runner_dir, rebuild_interval=3600
    )


def test_should_not_rebuild_when_fresh(tmp_path):
    source = _make_source_profile(tmp_path)
    runner_dir = tmp_path / "runner"
    chrome_runner.prepare_runner_profile(
        source, runner_dir, "Default", root_files=(), profile_files=("Preferences",), profile_dirs=("Network",)
    )
    chrome_runner._write_runner_meta(
        runner_dir,
        profile_name="Default",
        source_profile_dir=source,
        rebuilt_at=chrome_runner._now_ts(),
    )

    assert not chrome_runner.should_rebuild_runner_profile(
        runner_dir / "Default", runner_dir, rebuild_interval=3600
    )


def test_ensure_debug_browser_reuses_ready_browser_and_does_not_copy(monkeypatch, tmp_path):
    source = _make_source_profile(tmp_path)
    runner_dir = tmp_path / "runner"

    monkeypatch.setattr(chrome_runner, "is_debug_browser_ready", lambda port: True)
    monkeypatch.setattr(chrome_runner, "should_rebuild_runner_profile", lambda *args, **kwargs: False)

    copy_called = []

    def no_copy(*args, **kwargs):
        copy_called.append(True)
        raise AssertionError("should not copy when browser is ready and profile is fresh")

    monkeypatch.setattr(chrome_runner, "prepare_runner_profile", no_copy)

    chrome_runner.ensure_debug_browser(
        runner_dir,
        "Default",
        9222,
        "https://example.com",
        source_profile_dir=source,
    )

    assert not copy_called


def test_ensure_debug_browser_prepares_profile_and_launches_when_needed(monkeypatch, tmp_path):
    source = _make_source_profile(tmp_path)
    runner_dir = tmp_path / "runner"

    monkeypatch.setattr(chrome_runner, "is_debug_browser_ready", lambda port: False)
    monkeypatch.setattr(chrome_runner, "wait_for_debug_browser", lambda port, timeout_seconds=20: True)
    monkeypatch.setattr(chrome_runner, "_stop_chrome_for_profile_copy", lambda: None)
    monkeypatch.setattr(chrome_runner, "_resolve_default_browser_executable", lambda: ("chrome", "chrome.exe"))

    popen_calls = []

    def fake_popen(args, **kwargs):
        popen_calls.append(args)
        return object()

    monkeypatch.setattr(chrome_runner.subprocess, "Popen", fake_popen)

    chrome_runner.ensure_debug_browser(
        runner_dir,
        "Default",
        9222,
        "https://example.com",
        source_profile_dir=source,
        headless=True,
    )

    assert (runner_dir / "Default" / "Preferences").exists()
    assert popen_calls
    assert "--remote-debugging-port=9222" in popen_calls[0]
    assert "--headless=new" in popen_calls[0]


def test_force_rebuild_triggers_copy_and_launch(monkeypatch, tmp_path):
    source = _make_source_profile(tmp_path)
    runner_dir = tmp_path / "runner"

    monkeypatch.setattr(chrome_runner, "is_debug_browser_ready", lambda port: False)
    monkeypatch.setattr(chrome_runner, "wait_for_debug_browser", lambda port, timeout_seconds=20: True)
    monkeypatch.setattr(chrome_runner, "_stop_chrome_for_profile_copy", lambda: None)
    monkeypatch.setattr(chrome_runner, "_resolve_default_browser_executable", lambda: ("chrome", "chrome.exe"))
    monkeypatch.setattr(chrome_runner.subprocess, "Popen", lambda *args, **kwargs: object())

    chrome_runner.force_rebuild_debug_browser(
        runner_dir,
        "Default",
        9222,
        "https://example.com",
        source_profile_dir=source,
    )

    assert (runner_dir / "Default" / "Network" / "Cookies").read_bytes() == b"cookies"


def test_close_debug_browser_closes_tabs_and_kills_matching_processes(monkeypatch, tmp_path):
    import json as _json
    runner_dir = tmp_path / "runner"
    closed_tabs = []
    killed_pids = []

    monkeypatch.setattr(
        chrome_runner,
        "list_debug_tabs",
        lambda port: [{"id": "tab-1"}, {"id": "tab-2"}],
    )

    def fake_urlopen(url, timeout):
        closed_tabs.append(url)

        class _Response:
            def close(self):
                pass
        return _Response()

    monkeypatch.setattr(chrome_runner.urllib.request, "urlopen", fake_urlopen)

    process_payload = _json.dumps([{"ProcessId": 123, "CommandLine": f"chrome.exe --user-data-dir={runner_dir}"}])

    def fake_run(args, **kwargs):
        class _Result:
            stdout = process_payload
            stderr = ""
        return _Result()

    monkeypatch.setattr(chrome_runner.subprocess, "run", fake_run)
    monkeypatch.setattr(chrome_runner.subprocess, "Popen", lambda *args, **kwargs: None)

    original_run = chrome_runner.subprocess.run

    def taskkill_wrapper(args, **kwargs):
        if args[0] == "taskkill":
            killed_pids.append(args[2])
            return type("R", (), {"stdout": "", "stderr": ""})()
        return original_run(args, **kwargs)

    monkeypatch.setattr(chrome_runner.subprocess, "run", taskkill_wrapper)

    chrome_runner.close_debug_browser(runner_dir, 9222)

    assert len(closed_tabs) == 2
    assert killed_pids == ["123"]
