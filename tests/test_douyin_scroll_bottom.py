"""抖音收藏滚动到底的几何判定：滚动卡住时绝不能被当成「扫完了」。"""

from connectors.douyin.favorites import _at_scroll_bottom


def test_bottom_when_container_scrolled_to_end():
    assert _at_scroll_bottom({"top": 900.0, "height": 1000, "client": 100}) is True
    assert _at_scroll_bottom({"top": 940.0, "height": 1000, "client": 100}) is True


def test_not_bottom_when_content_remains_below():
    assert _at_scroll_bottom({"top": 500.0, "height": 1000, "client": 100}) is False


def test_unreadable_state_is_never_bottom():
    assert _at_scroll_bottom(None) is False


def test_non_scrollable_container_is_never_bottom():
    # 高度等于视口说明取错了容器（或列表本身没内容），不能据此判到底。
    assert _at_scroll_bottom({"top": 0.0, "height": 100, "client": 100}) is False
