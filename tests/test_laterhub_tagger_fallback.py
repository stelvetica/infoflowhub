"""laterhub 打标签的主/备切换口径。

核心回归：主家 4xx 拒绝、响应畸形、content 为空时**必须**去试备用——旧实现在这些错误上
直接 `break`，备用从不上场（这就是"配了备用却没效果"的原因）。不触真实网络：requests.post
被打桩。
"""

from __future__ import annotations

import json

import requests
import pytest

from apps.laterhub import tagger as tagger_mod
from apps.laterhub.tagger import (
    ContentTagger,
    LLMConfig,
    TaggerPermanentError,
    TaggerTemporaryError,
)

PRIMARY = LLMConfig(base_url="https://primary.test", api_key="k1", model="glm-5.3-flash")
BACKUP = LLMConfig(base_url="https://backup.test", api_key="k2", model="deepseek-flash")


class FakeResponse(requests.Response):
    """真 requests.Response 子类：分类器用 isinstance(response, Response) 读状态码。"""

    def __init__(self, status_code: int = 200, payload: dict | None = None, text: str = "") -> None:
        super().__init__()
        self.status_code = status_code
        self._content = json.dumps(payload).encode("utf-8") if payload is not None else text.encode("utf-8")


def _ok(content: str) -> FakeResponse:
    return FakeResponse(200, {"choices": [{"message": {"content": content}}]})


def _null_content() -> FakeResponse:
    return FakeResponse(200, {"choices": [{"message": {"content": None}}]})


def _http_error(status_code: int) -> requests.HTTPError:
    response = FakeResponse(status_code, text='{"error":"boom"}')
    return requests.HTTPError(f"HTTP {status_code}", response=response)


def _patch_post(monkeypatch, per_model: dict) -> list[str]:
    """按 payload['model'] 返回预设响应（Exception 则抛出），并记录调用顺序。"""
    calls: list[str] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        model = json["model"]
        calls.append(model)
        result = per_model[model]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(tagger_mod.requests, "post", fake_post)
    return calls


def test_primary_http_400_falls_through_to_backup(monkeypatch):
    """主家 400（网关瞬时拒绝）必须换备用，而不是直接判死。"""
    calls = _patch_post(
        monkeypatch,
        {PRIMARY.model: _http_error(400), BACKUP.model: _ok('{"tags": ["技术_AI/工具/开发"]}')},
    )

    tags = ContentTagger(PRIMARY, backup_config=BACKUP).tag(title="标题", source="douyin_favorite")

    assert tags == "技术_AI/工具/开发"
    assert calls == [PRIMARY.model, BACKUP.model]


def test_primary_null_content_falls_through_to_backup(monkeypatch):
    """content 为 null（思考模型耗尽 max_tokens）也必须换备用。"""
    calls = _patch_post(
        monkeypatch,
        {PRIMARY.model: _null_content(), BACKUP.model: _ok('{"tags": ["心理_认知/学习方法"]}')},
    )

    tags = ContentTagger(PRIMARY, backup_config=BACKUP).tag(title="标题", source="douyin_favorite")

    assert tags == "心理_认知/学习方法"
    assert calls == [PRIMARY.model, BACKUP.model]


def test_primary_auth_error_still_tries_backup(monkeypatch):
    """主家 401 也照样试备用——备用用的是另一套 key，很可能就能用。"""
    calls = _patch_post(
        monkeypatch,
        {PRIMARY.model: _http_error(401), BACKUP.model: _ok('{"tags": ["娱乐_文学/艺术"]}')},
    )

    tags = ContentTagger(PRIMARY, backup_config=BACKUP).tag(title="标题", source="bilibili_watchlater")

    assert tags == "娱乐_文学/艺术"
    assert calls == [PRIMARY.model, BACKUP.model]


def test_primary_success_never_calls_backup(monkeypatch):
    """主家成功就不该多花一次备用调用。"""
    calls = _patch_post(monkeypatch, {PRIMARY.model: _ok('{"tags": ["技术_AI/工具/开发"]}')})

    tags = ContentTagger(PRIMARY, backup_config=BACKUP).tag(title="标题", source="douyin_favorite")

    assert tags == "技术_AI/工具/开发"
    assert calls == [PRIMARY.model]


def test_both_auth_errors_are_permanent(monkeypatch):
    """两家都是鉴权错：本轮不再重试（下轮仍会统一补标签）。"""
    _patch_post(monkeypatch, {PRIMARY.model: _http_error(401), BACKUP.model: _http_error(401)})

    with pytest.raises(TaggerPermanentError) as excinfo:
        ContentTagger(PRIMARY, backup_config=BACKUP).tag(title="标题", source="douyin_favorite")

    message = str(excinfo.value)
    assert "primary" in message and "backup" in message
    assert "401" in message


def test_both_malformed_responses_are_temporary(monkeypatch):
    """两家都返回畸形内容：判为可重试（下轮统一补标签），且文案要带两家。"""
    _patch_post(
        monkeypatch,
        {PRIMARY.model: _ok("这不是 JSON"), BACKUP.model: _ok("也不是 JSON")},
    )

    with pytest.raises(TaggerTemporaryError) as excinfo:
        ContentTagger(PRIMARY, backup_config=BACKUP).tag(title="标题", source="douyin_favorite")

    message = str(excinfo.value)
    assert "primary" in message and "backup" in message


def test_null_content_error_is_readable_not_attribute_error(monkeypatch):
    """content 为 null 时必须给可读错误，不能退化成 'NoneType' object has no attribute 'strip'。"""
    _patch_post(monkeypatch, {PRIMARY.model: _null_content(), BACKUP.model: _null_content()})

    with pytest.raises(TaggerTemporaryError) as excinfo:
        ContentTagger(PRIMARY, backup_config=BACKUP).tag(title="标题", source="douyin_favorite")

    message = str(excinfo.value)
    assert "模型返回空 content" in message
    assert "NoneType" not in message
    assert "has no attribute" not in message


def test_no_backup_configured_reports_temporary(monkeypatch):
    """没配备用时不炸：单家 400 仍判为可重试。"""
    _patch_post(monkeypatch, {PRIMARY.model: _http_error(400)})

    with pytest.raises(TaggerTemporaryError):
        ContentTagger(PRIMARY).tag(title="标题", source="douyin_favorite")


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_http_error(401), True),
        (_http_error(403), True),
        (_http_error(404), True),
        (_http_error(400), False),
        (_http_error(429), False),
        (_http_error(500), False),
        (requests.Timeout("timeout"), False),
        (requests.ConnectionError("reset"), False),
        (RuntimeError("模型返回空 content（思考模型可能耗尽 max_tokens）"), False),
        (ValueError("不支持的标签返回格式: xxx"), False),
    ],
)
def test_permanent_failure_classification(exc, expected):
    """永久失败只认"这家自身配置/凭证坏了"：401/403/404；其余一律可重试（含 400）。"""
    assert ContentTagger._is_permanent_failure(exc) is expected
