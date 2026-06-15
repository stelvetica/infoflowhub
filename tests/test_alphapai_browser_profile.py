from __future__ import annotations

from connectors.alphapai import browser as alphapai_browser


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
