"""_wait_ready 就绪判定的回归护栏。

用假 page 覆盖 _pick_list_text 的调用面，不依赖真实浏览器/bsk。
"""

from __future__ import annotations

import pytest

from connectors.alphapai import feed
from connectors.alphapai.feed import LIST_SELECTOR, _wait_ready


# 真实页面（bsk 每秒采样）里 4.8s 之后的 body 文本：品牌文案已随开场弹窗隐藏而消失，
# 但报告列表一直可解析。以此作为「页面已就绪」的实测样本。
BODY_NO_BRAND = (
    "搜索\nPaiWork\n发现\nOpenPai\n市场报告\n"
    "国内9月29日晨会版| 兆易上调长鑫采购额度、长鑫科技349亿研发\n09-29 11:11\n"
    "全球9月29日全球版| 美联储降息预期升温与全球资金流向观察\n09-29 08:30\n"
    "国内9月28日晚间版| 半导体设备国产化进程与订单跟踪\n09-28 20:15\n"
    "全球9月28日午间版| 港股通资金流向与科技板块估值修复\n09-28 12:00\n"
    "自选\n我的\n设置\n"
)

LIST_WITH_ENTRIES = (
    "国内9月29日晨会版| 兆易上调长鑫采购额度、长鑫科技349亿研发\n09-29 11:11\n"
    "全球9月29日全球版| 美联储降息预期升温与全球资金流向观察\n09-29 08:30\n"
)


class FakeLocator:
    def __init__(self, page: "FakePage", selector: str) -> None:
        self._page = page
        self._selector = selector

    def count(self) -> int:
        if self._selector == "body":
            return 1
        return len(self._page.list_texts)

    def nth(self, index: int) -> "FakeLocator":
        return FakeLocator(self._page, f"{self._selector}#{index}")

    def inner_text(self, timeout: int | None = None) -> str:
        if self._selector == "body":
            return self._page.body_text
        if "#" in self._selector:
            index = int(self._selector.rsplit("#", 1)[1])
            return self._page.list_texts[index]
        raise RuntimeError("inner_text 只能作用于 body 或 nth 定位到的容器")


class FakePage:
    def __init__(self, body_text: str, list_texts: list[str]) -> None:
        self.body_text = body_text
        self.list_texts = list_texts

    def locator(self, selector: str) -> FakeLocator:
        assert selector in ("body", LIST_SELECTOR), selector
        return FakeLocator(self, selector)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(feed._time, "sleep", lambda _seconds: None)


def test_ready_when_list_parsable_without_brand_text():
    """品牌文案只在开场弹窗可见，列表可解析即视为就绪（2026-09-29 11:11 误报的真因）。"""
    page = FakePage(BODY_NO_BRAND, [LIST_WITH_ENTRIES])
    assert _wait_ready(page, timeout_ms=200) is True


def test_not_ready_when_body_empty_and_no_container():
    page = FakePage("", [])
    assert _wait_ready(page, timeout_ms=200) is False


def test_not_ready_when_body_long_but_container_has_no_entries():
    page = FakePage(BODY_NO_BRAND, ["暂无数据"])
    assert _wait_ready(page, timeout_ms=200) is False
