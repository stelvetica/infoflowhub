from __future__ import annotations

import time

from apps.laterhub import pipeline


def test_fetcher_normal_returns_result(monkeypatch):
    """正常 fetcher：看门狗放行结果。"""
    def ok_fetcher(env_path, *, session=None):
        return [{"url": "u1", "title": "t1", "source": "s1"}]

    got = pipeline._run_fetcher_with_timeout("测试源", ok_fetcher)
    assert got == [{"url": "u1", "title": "t1", "source": "s1"}]


def test_fetcher_timeout_kills_browser_and_raises(monkeypatch):
    """挂死 fetcher：超时后杀浏览器并抛 RuntimeError，不被卡死。"""
    monkeypatch.setattr(pipeline, "SOURCE_FETCH_TIMEOUT_SECONDS", 1)

    killed = []

    def fake_terminate(label: str) -> None:
        killed.append(label)

    def hang_fetcher(env_path, *, session=None):
        time.sleep(60)
        return []

    monkeypatch.setattr(pipeline, "_terminate_source_session", fake_terminate)
    try:
        pipeline._run_fetcher_with_timeout("小黑盒收藏", hang_fetcher)
        assert False, "should have raised"
    except RuntimeError as exc:
        assert "未完成" in str(exc)
    assert killed == ["小黑盒收藏"]


def test_fetcher_exception_propagates(monkeypatch):
    """fetcher 内部抛错：原样上传，不触发杀浏览器。"""
    killed = []

    def boom_fetcher(env_path, *, session=None):
        raise ValueError("boom")

    monkeypatch.setattr(pipeline, "_terminate_source_session", lambda label: killed.append(label))
    try:
        pipeline._run_fetcher_with_timeout("抖音收藏", boom_fetcher)
        assert False, "should have raised"
    except ValueError as exc:
        assert str(exc) == "boom"
    assert killed == []


def test_source_debug_port_mapping():
    """浏览器源有对应 debug port，非浏览器源为 None。"""
    assert pipeline._source_debug_port("抖音收藏") == 9224
    assert pipeline._source_debug_port("小黑盒收藏") == 9225
    assert pipeline._source_debug_port("B 站稍后看") is None