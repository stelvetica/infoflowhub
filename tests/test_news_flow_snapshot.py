"""流程维度产物快照：mtime 基线判定 + news_scope=flow/all 的范围切换。"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from apps.subscriptions import rss_db
from web.services import auto_runner, views

STARTED_AT = "2026-09-22 08:10:00"
STARTED_TS = datetime.strptime(STARTED_AT, auto_runner.DT_FORMAT).timestamp()
NEWS_MD = "新闻影响分析_2026-09-22.md"
ANNOUNCE_MD = "公告清单_2026-09-22.md"
ANNOUNCE_PNG = "公告清单_2026-09-22.png"
TODAY_NEWS_MD = "今日新闻.md"


@pytest.fixture()
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(rss_db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(rss_db, "DB_PATH", tmp_path / "subscriptions.sqlite3")
    return tmp_path / "subscriptions.sqlite3"


def _write_report(reports: Path, name: str, mtime: float) -> Path:
    path = reports / name
    path.write_text("# 产物\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    reports = tmp_path / "data" / "reports"
    reports.mkdir(parents=True)
    runtime = tmp_path / "runtime" / "automation_runtime.json"
    monkeypatch.setattr(views, "REPORTS_DIR", reports)
    monkeypatch.setattr(views, "AUTOMATION_RUNTIME_PATH", runtime)
    return {"reports": reports, "runtime": runtime}


def _write_state(runtime: Path, flow: dict | None) -> None:
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_text(json.dumps({"last_status": "success", "flow": flow} if flow else {}), encoding="utf-8")


# ── mtime 基线判定 ──────────────────────────────────────────────


def test_flow_products_uses_mtime_baseline(env: dict[str, Path]):
    _write_report(env["reports"], NEWS_MD, STARTED_TS - 60)  # 流程开始前生成，不算本次产物
    _write_report(env["reports"], ANNOUNCE_MD, STARTED_TS + 60)  # 流程内生成，算
    _write_report(env["reports"], TODAY_NEWS_MD, STARTED_TS + 1)
    names = [item["name"] for item in auto_runner._flow_products(STARTED_AT)]
    # 顺序跟随 REPORT_PATTERNS（公告清单在今日新闻之前）
    assert names == [ANNOUNCE_MD, TODAY_NEWS_MD]
    assert auto_runner._flow_products("非法时间") == []


def test_flow_products_entry_shape(env: dict[str, Path]):
    _write_report(env["reports"], ANNOUNCE_PNG, STARTED_TS + 60)
    [product] = auto_runner._flow_products(STARTED_AT)
    assert product == {"name": ANNOUNCE_PNG, "kind": "公告图片", "is_image": True}


# ── news_scope 范围切换 ─────────────────────────────────────────


def test_news_scope_flow_returns_only_current_flow_products(env: dict[str, Path], temp_db: Path):
    _write_report(env["reports"], NEWS_MD, STARTED_TS - 600)
    _write_report(env["reports"], ANNOUNCE_MD, STARTED_TS + 60)
    _write_report(env["reports"], TODAY_NEWS_MD, STARTED_TS + 60)
    _write_state(env["runtime"], {"flow_id": "x", "status": "success", "products": [{"name": ANNOUNCE_MD, "kind": "公告清单", "is_image": False}]})
    view = views.get_news_view({"news_scope": "flow"})
    assert view["scope"] == "flow"
    assert [row["name"] for row in view["rows"]] == [ANNOUNCE_MD]
    # 补全成完整行：link/read_key/时间字段都要有
    row = view["rows"][0]
    assert row["link"].startswith("/reports/view/")
    assert row["read_key"] == f"report:{ANNOUNCE_MD}"
    assert row["display_time"] and row["sort_time"]


def test_news_scope_defaults_to_flow(env: dict[str, Path], temp_db: Path):
    _write_report(env["reports"], NEWS_MD, STARTED_TS + 60)
    _write_state(env["runtime"], {"flow_id": "x", "status": "success", "products": [{"name": NEWS_MD, "kind": "新闻影响分析", "is_image": False}]})
    view = views.get_news_view({})
    assert view["scope"] == "flow"
    assert [row["name"] for row in view["rows"]] == [NEWS_MD]


def test_news_scope_all_returns_whole_whitelist(env: dict[str, Path], temp_db: Path):
    for name in (NEWS_MD, ANNOUNCE_MD, ANNOUNCE_PNG, TODAY_NEWS_MD):
        _write_report(env["reports"], name, STARTED_TS - 600)
    view = views.get_news_view({"news_scope": "all"})
    assert view["scope"] == "all"
    assert view["filtered_total"] == 4


def test_news_scope_flow_empty_without_flow_record(env: dict[str, Path], temp_db: Path):
    _write_report(env["reports"], NEWS_MD, STARTED_TS + 60)
    _write_state(env["runtime"], None)
    assert views.get_flow_state() == {}
    assert views.get_news_view({"news_scope": "flow"})["filtered_total"] == 0


def test_news_scope_flow_empty_while_running_without_products(env: dict[str, Path], temp_db: Path):
    _write_report(env["reports"], NEWS_MD, STARTED_TS + 60)
    _write_state(env["runtime"], {"flow_id": "x", "status": "running", "products": []})
    assert views.get_news_view({"news_scope": "flow"})["filtered_total"] == 0


def test_news_scope_flow_skips_deleted_product_files(env: dict[str, Path], temp_db: Path):
    _write_state(env["runtime"], {"flow_id": "x", "status": "success", "products": [{"name": NEWS_MD, "kind": "新闻影响分析", "is_image": False}]})
    assert views.get_news_view({"news_scope": "flow"})["filtered_total"] == 0


# ── 流程结束后打开面板（产物不再自动打开） ──────────────────────


@pytest.fixture()
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """截住浏览器唤起，并清掉环境变量干扰。"""
    monkeypatch.delenv("INFOFLOW_OPEN_PANEL", raising=False)
    calls: list[str] = []
    monkeypatch.setattr(auto_runner.webbrowser, "open", calls.append)
    return calls


def test_open_panel_opens_infoflow_url(opened: list[str]):
    auto_runner._open_panel()
    assert opened == [auto_runner.INFOFLOW_WEB_URL]


def test_open_panel_respects_disable_switch(opened: list[str], monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("INFOFLOW_OPEN_PANEL", "0")
    auto_runner._open_panel()
    assert opened == []


def test_open_panel_swallows_browser_failure(opened: list[str], monkeypatch: pytest.MonkeyPatch):
    def boom(_url: str) -> None:
        raise RuntimeError("no default browser")

    monkeypatch.setattr(auto_runner.webbrowser, "open", boom)
    auto_runner._open_panel()  # 无 GUI 环境不能把流程带崩


def _run_morning_once(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, *, fetch_error: Exception | None = None) -> None:
    """把 _do_run_morning 的抓取与 skill 全部换掉，只观察收尾行为。"""

    def fake_fetch_now(*_args, **_kwargs):
        if fetch_error:
            raise fetch_error
        return {"success_sources": 1}

    monkeypatch.setattr(auto_runner, "fetch_now", fake_fetch_now)
    monkeypatch.setattr(auto_runner, "fetch_laterhub_now", lambda *a, **k: {"fetched_sources": 1})
    monkeypatch.setattr(auto_runner, "run_skill", lambda name, *, cwd, timeout=None: {"name": name, "status": "success", "started_at": "", "finished_at": "", "error": ""})
    runner = auto_runner.AutoRunner()
    now = datetime.now()
    asyncio.run(runner._do_run_morning(now, now.date().isoformat()))


def test_morning_flow_opens_panel_after_success(env: dict[str, Path], temp_db: Path, opened: list[str], monkeypatch: pytest.MonkeyPatch):
    _run_morning_once(env, monkeypatch)
    assert opened == [auto_runner.INFOFLOW_WEB_URL]


def test_morning_flow_opens_panel_even_when_fetch_fails(env: dict[str, Path], temp_db: Path, opened: list[str], monkeypatch: pytest.MonkeyPatch):
    _run_morning_once(env, monkeypatch, fetch_error=RuntimeError("抓取炸了"))
    assert opened == [auto_runner.INFOFLOW_WEB_URL]


def test_morning_flow_records_run_date_and_result(env: dict[str, Path], temp_db: Path, opened: list[str], monkeypatch: pytest.MonkeyPatch):
    """守自动路径的 last_run_date / last_result 写入。

    last_run_date 是当日去重依据（漏写则 _do_run_morning 每天重复跑），
    last_result 是面板头部数据源。二者在重构后改为 _finish_flow 的条件写入，
    此处确保自动路径调用方确实传了 today/result。给一个未来 mtime 的白名单产物，
    让本次流程被判 success 而非「零产物降级」。
    """
    _write_report(env["reports"], ANNOUNCE_MD, datetime.now().timestamp() + 3600)
    _run_morning_once(env, monkeypatch)

    state = views.read_json(env["runtime"], {})
    assert state["last_run_date"] == datetime.now().date().isoformat()
    assert state["last_result"] == {"subscriptions": {"success_sources": 1}, "laterhub": {"fetched_sources": 1}}
    assert state["flow"]["status"] == "success"


# ── 调度循环韧性（H1）───────────────────────────────────────────


def test_loop_survives_exception_from_maybe_run_morning(monkeypatch: pytest.MonkeyPatch):
    """_maybe_run_morning 抛异常不得让常驻循环退出（否则后台任务静默死亡）。"""
    runner = auto_runner.AutoRunner()
    calls: list[int] = []

    async def boom() -> None:
        calls.append(1)
        if len(calls) >= 2:
            runner._stopped.set()
        raise RuntimeError("炸了")

    monkeypatch.setattr(runner, "_maybe_run_morning", boom)
    monkeypatch.setattr(auto_runner, "CHECK_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(auto_runner, "CHECK_INTERVAL_SECONDS_MORNING", 0)
    asyncio.run(runner._loop())  # 修复前：异常冲出 → asyncio.run 直接报错
    assert len(calls) == 2  # 第一轮异常后循环仍继续，跑到第二轮才停


# ── 全成功但零产物降级（B1）─────────────────────────────────────


def test_finalize_flow_downgrades_success_when_no_products(env: dict[str, Path]):
    runner = auto_runner.AutoRunner()
    state: dict = {"flow": {}}
    runner._finalize_flow(state, STARTED_AT, status="success", skills=[{"name": "portfolio-news", "status": "success"}])
    assert state["flow"]["status"] == "partial"
    assert "未检测到新增产物" in state["flow"]["error"]


def test_finalize_flow_keeps_success_when_skill_stage_disabled(env: dict[str, Path]):
    """技能阶段被开关关闭（skills 为空）不是异常，不得降级。"""
    runner = auto_runner.AutoRunner()
    state: dict = {"flow": {}}
    runner._finalize_flow(state, STARTED_AT, status="success", skills=[])
    assert state["flow"]["status"] == "success"
    assert state["flow"]["error"] == ""


def test_finalize_flow_keeps_success_when_products_detected(env: dict[str, Path]):
    runner = auto_runner.AutoRunner()
    _write_report(env["reports"], ANNOUNCE_MD, STARTED_TS + 60)
    state: dict = {"flow": {}}
    runner._finalize_flow(state, STARTED_AT, status="success", skills=[{"name": "portfolio-news", "status": "success"}])
    assert state["flow"]["status"] == "success"
    assert state["flow"]["error"] == ""


# ── 运行期逐条落盘 skills（M4）──────────────────────────────────


def test_skills_persisted_after_each_skill(env: dict[str, Path], temp_db: Path, opened: list[str], monkeypatch: pytest.MonkeyPatch):
    """每个 skill 跑完即落盘 flow.skills，运行期状态条才能显示真实阶段。"""
    seen: list[list[dict]] = []

    def fake_run_skill(name, *, cwd, timeout=None):
        seen.append(list(views.read_json(env["runtime"], {}).get("flow", {}).get("skills", [])))
        return {"name": name, "status": "success", "started_at": "", "finished_at": "", "error": ""}

    monkeypatch.setattr(auto_runner, "run_skill", fake_run_skill)
    monkeypatch.setattr(auto_runner, "fetch_now", lambda *a, **k: {"success_sources": 1})
    monkeypatch.setattr(auto_runner, "fetch_laterhub_now", lambda *a, **k: {"fetched_sources": 1})
    runner = auto_runner.AutoRunner()
    now = datetime.now()
    asyncio.run(runner._do_run_morning(now, now.date().isoformat()))
    assert seen[0] == []  # 第一个 skill 开始时尚无已完成技能
    assert [item["name"] for item in seen[1]] == [auto_runner.SKILLS[0]]  # 第二个开始时第一个已落盘


# ── 环境变量开关解析（L4）───────────────────────────────────────


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "disable", "disabled", "NO", "Off"])
def test_open_panel_disable_variants(opened: list[str], monkeypatch: pytest.MonkeyPatch, value: str):
    monkeypatch.setenv("INFOFLOW_OPEN_PANEL", value)
    auto_runner._open_panel()
    assert opened == []


@pytest.mark.parametrize("value", ["", "1", "yes", "on", "true"])
def test_open_panel_enable_variants(opened: list[str], monkeypatch: pytest.MonkeyPatch, value: str):
    monkeypatch.setenv("INFOFLOW_OPEN_PANEL", value)
    auto_runner._open_panel()
    assert opened == [auto_runner.INFOFLOW_WEB_URL]


def test_skill_stage_disabled_variants(monkeypatch: pytest.MonkeyPatch):
    for value in ("no", "off", "disabled"):
        monkeypatch.setenv("INFOFLOW_SKILL_RUNNER", value)
        assert auto_runner.AutoRunner._skill_stage_enabled() is False
    monkeypatch.setenv("INFOFLOW_SKILL_RUNNER", "1")
    assert auto_runner.AutoRunner._skill_stage_enabled() is True


# ── 手动补跑 run_skills_now 登记一次流程 ────────────────────────


def _skill_ok(env: dict[str, Path]):
    """返回一个 skill 假实现：成功并写一个白名单产物，供产物扫描命中。"""

    def fake_run_skill(name, *, cwd, timeout=None):
        _write_report(env["reports"], ANNOUNCE_MD, datetime.now().timestamp())
        return {"name": name, "status": "success", "started_at": "", "finished_at": "", "error": ""}

    return fake_run_skill


def test_run_skills_now_records_flow(env: dict[str, Path], temp_db: Path, opened: list[str], monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(auto_runner, "run_skill", _skill_ok(env))
    runner = auto_runner.AutoRunner()
    asyncio.run(runner.run_skills_now())

    state = views.read_json(env["runtime"], {})
    flow = state["flow"]
    assert flow["status"] == "success"
    assert [item["name"] for item in flow["products"]] == [ANNOUNCE_MD]
    assert flow["finished_at"]
    assert state["last_run_date"] == datetime.now().date().isoformat()


def test_run_skills_now_marks_partial_when_skill_fails(env: dict[str, Path], temp_db: Path, opened: list[str], monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        auto_runner,
        "run_skill",
        lambda name, *, cwd, timeout=None: {"name": name, "status": "failed", "started_at": "", "finished_at": "", "error": "boom"},
    )
    runner = auto_runner.AutoRunner()
    asyncio.run(runner.run_skills_now())

    state = views.read_json(env["runtime"], {})
    assert state["flow"]["status"] == "partial"
    # 与自动路径一致：flow 记 partial，但 last_status 仍代表流程本身跑完
    assert state["last_status"] == "success"


def test_run_skills_now_keeps_existing_last_result(env: dict[str, Path], temp_db: Path, opened: list[str], monkeypatch: pytest.MonkeyPatch):
    existing = {"subscriptions": {"success_sources": 1}}
    env["runtime"].parent.mkdir(parents=True, exist_ok=True)
    env["runtime"].write_text(json.dumps({"last_result": existing}), encoding="utf-8")
    monkeypatch.setattr(auto_runner, "run_skill", _skill_ok(env))

    runner = auto_runner.AutoRunner()
    asyncio.run(runner.run_skills_now())

    assert views.read_json(env["runtime"], {})["last_result"] == existing