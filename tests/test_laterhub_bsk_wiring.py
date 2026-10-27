"""laterhub 抓取链路的**全仓口径**结构守卫（两个收藏源走 bsk，B站稍后看走官方 API）。

现有 tests/test_bsk_backend_wiring.py::test_no_enabled_source_falls_through_to_copy 只遍历
「源表」，看不见 laterhub 这条平行抓取路径——抖音收藏曾静默 15 天没被拦住。本文件按源码
静态扫描 + 反射，把「laterhub 已无 copy 后端」钉成结构约束。

不引入新依赖：只用 pathlib / inspect / pytest。
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from connectors.bilibili import fetch_bilibili_watchlater
from connectors.douyin import fetch_douyin_favorites
from connectors.weread import fetch_weread_shelf
from connectors.xiaoheihe import fetch_xiaoheihe_favorites

REPO_ROOT = Path(__file__).resolve().parents[1]

# 全仓口径：凡触碰 laterhub 抓取链路的源码文件都在此登记，新增文件须补进来。
GUARDED_FILES = {
    "apps/laterhub/pipeline.py": REPO_ROOT / "apps" / "laterhub" / "pipeline.py",
    "apps/laterhub/workflow.py": REPO_ROOT / "apps" / "laterhub" / "workflow.py",
    "connectors/douyin/favorites.py": REPO_ROOT / "connectors" / "douyin" / "favorites.py",
    "connectors/xiaoheihe/feed.py": REPO_ROOT / "connectors" / "xiaoheihe" / "feed.py",
    "connectors/weread/shelf.py": REPO_ROOT / "connectors" / "weread" / "shelf.py",
    "connectors/bilibili/api.py": REPO_ROOT / "connectors" / "bilibili" / "api.py",
}

# 迁移后不得再出现在上述文件里的 copy 语义痕迹。
FORBIDDEN_TOKENS = (
    "CopyRunnerSession",
    "LATERHUB_RUNNER_DIR",
    "LATERHUB_RUNNER_DEBUG_PORT",
    "close_debug_browser",
    "_reopen_user_chrome",
    "9224",
    "9225",
)

LATERHUB_FETCHERS = {
    "B 站稍后看": fetch_bilibili_watchlater,
    "抖音收藏": fetch_douyin_favorites,
    "小黑盒收藏": fetch_xiaoheihe_favorites,
    "微信读书书架": fetch_weread_shelf,
}

# 走 bsk（需要浏览器会话）的源；B 站稍后看走官方 API 直连，不需要也不接受 session。
BROWSER_FETCHERS = {
    "抖音收藏": fetch_douyin_favorites,
    "小黑盒收藏": fetch_xiaoheihe_favorites,
    "微信读书书架": fetch_weread_shelf,
}


def find_forbidden_tokens(source: str, tokens: tuple[str, ...] = FORBIDDEN_TOKENS) -> list[str]:
    """纯函数：返回 source 中命中的违规 token。抽出来是为了守卫自检。"""
    return [token for token in tokens if token in source]


@pytest.mark.parametrize("label", sorted(GUARDED_FILES))
def test_guarded_source_has_no_copy_tokens(label):
    """全仓口径：这些文件都不该再出现任何 copy 后端痕迹。"""
    source = GUARDED_FILES[label].read_text(encoding="utf-8")
    violations = find_forbidden_tokens(source)
    assert violations == [], f"{label} 仍残留旧 copy 痕迹: {violations}"


def test_guard_is_effective_on_injected_sample():
    """守卫自检：喂进含违规 token 的源码必须判定违规，避免守卫恒真。"""
    sample = "from connectors._shared.chrome_runner import CopyRunnerSession\n"
    assert find_forbidden_tokens(sample) == ["CopyRunnerSession"]

    # 干净样例不得误报（含正常 bsk 用法）。
    clean = "from connectors._shared.bsk_session import BskRunnerSession\n"
    assert find_forbidden_tokens(clean) == []


def test_pipeline_drops_removed_copy_helpers():
    """旧 copy 专用兜底函数必须已从 pipeline 删除（符号与源码双查）。"""
    import apps.laterhub.pipeline as pipeline

    assert not hasattr(pipeline, "_source_debug_port")
    assert not hasattr(pipeline, "_terminate_source_session")
    source = GUARDED_FILES["apps/laterhub/pipeline.py"].read_text(encoding="utf-8")
    assert "_source_debug_port" not in source
    assert "_terminate_source_session" not in source


@pytest.mark.parametrize("label", sorted(BROWSER_FETCHERS))
def test_laterhub_browser_fetcher_accepts_session_keyword(label):
    """走 bsk 的抓取器必须接受 session 关键字参数（共用同一浏览器会话）。"""
    fetcher = BROWSER_FETCHERS[label]
    signature = inspect.signature(fetcher)
    assert "session" in signature.parameters, f"{label} 抓取器缺少 session 参数"
    assert signature.parameters["session"].kind is inspect.Parameter.KEYWORD_ONLY, (
        f"{label} 的 session 必须是仅关键字参数"
    )


@pytest.mark.parametrize("label", sorted(BROWSER_FETCHERS))
def test_laterhub_browser_fetcher_session_is_bsk_runner(label):
    """走 bsk 的抓取器源码必须从 bsk_session 模块导入会话类型。"""
    module_path = Path(inspect.getsourcefile(BROWSER_FETCHERS[label]))
    source = module_path.read_text(encoding="utf-8")
    assert "from connectors._shared.bsk_session import" in source, f"{label} 未从 bsk_session 导入"
    assert "BskRunnerSession" in source, f"{label} 未使用 BskRunnerSession"


def test_bilibili_watchlater_stays_on_http_api():
    """B 站稍后看走官方 API 直连：不接 session、不拉浏览器（比 bsk 更快更稳）。"""
    signature = inspect.signature(fetch_bilibili_watchlater)
    assert "session" not in signature.parameters, "B 站稍后看不该要 session——它走 HTTP 直连"
    module_path = Path(inspect.getsourcefile(fetch_bilibili_watchlater))
    source = module_path.read_text(encoding="utf-8")
    assert "BilibiliWatchLaterFetcher" in source
    assert "BILIBILI_API_CANDIDATES" in source
    # 该文件仍服务订阅流 B站动态的 bsk 页内取数，故只断言「稍后看函数体内」不含浏览器取数痕迹。
    watchlater_body = source.split("class BilibiliWatchLaterFetcher")[1].split("class BilibiliApiSession")[0]
    for token in ("BskRunnerSession", "acquire_page", "evaluate("):
        assert token not in watchlater_body, f"稍后看 HTTP 路径不该出现 {token}"


def test_pipeline_treats_bilibili_as_non_browser_source():
    """B 站稍后看必须在 pipeline 里标为非浏览器源：bsk 会话起不来时它仍要被抓。"""
    source = GUARDED_FILES["apps/laterhub/pipeline.py"].read_text(encoding="utf-8")
    assert '"B 站稍后看", fetch_bilibili_watchlater, False' in source
    assert '"抖音收藏", fetch_douyin_favorites, True' in source
    assert '"小黑盒收藏", fetch_xiaoheihe_favorites, True' in source
    assert '"微信读书书架", fetch_weread_shelf, True' in source


def test_pipeline_uses_shared_bsk_session():
    """两个浏览器源共用会话的事实：pipeline 必须构造 BskRunnerSession 且不再出现 copy 符号。"""
    source = GUARDED_FILES["apps/laterhub/pipeline.py"].read_text(encoding="utf-8")
    assert "BskRunnerSession" in source
    assert find_forbidden_tokens(source) == []


def test_manual_fetch_explicitly_enables_every_source():
    """页面「立即抓取」必须逐源显式开启：不能靠默认值，否则改默认时会静默丢源。"""
    source = (REPO_ROOT / "web" / "services" / "fetch_runtime.py").read_text(encoding="utf-8")
    for token in ("fetch_bilibili=True", "fetch_douyin=True", "fetch_xiaoheihe=True", "fetch_weread=True"):
        assert token in source
