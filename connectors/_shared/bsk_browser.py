"""bsk 浏览器适配层：用 bsk CLI（browser-skill daemon）驱动用户已登录的 Chromium，
向 connector 提供最小 Playwright 风格 page facade。

设计边界（KISS）：只实现蓝宝书 connector 实际用到的调用面
（page.url / goto / content / close / locator(...).{count,nth,first,inner_text,
all_inner_texts,get_by_text,click} / page.get_by_text），不做通用 Playwright 替代品。

会话生命周期由 BskBrowser 统一持有，page.close() 不结束会话（同一 session 可复用）；
任何异常/超时/业务结束都通过 `with BskBrowser() as browser:` 的 try/finally 兜底 stop。
日志只记命令类型、URL、退出码、耗时与错误分类，绝不记录命令完整输出（可能含凭据）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence


DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_REQUEST_HELP_TIMEOUT_SECONDS = 30.0
BSK_BIN_NAME = "bsk"


class BskError(Exception):
    """bsk 适配层错误基类。"""


class BskTimeout(BskError):
    """子进程硬超时，或 bsk 自身报告超时（退出码 4 / code=timeout）。"""


class BskHelpTimeout(BskTimeout):
    """疑似 request_help 阻塞（登录/验证码等人）导致的超时，上层应判定本次抓取失败。"""


class BskCommandError(BskError):
    """bsk 命令返回非零退出码，或 evaluate 返回 ok=false。"""


class BskSessionError(BskError):
    """session 启动失败（拿不到 session_id）。"""


@dataclass(frozen=True)
class BskResult:
    returncode: int
    stdout: str
    stderr: str


def resolve_bsk_command() -> tuple[str, ...]:
    # 不硬编码路径：先查 PATH，再退回 uv 安装目录下的默认位置（~/.local/bin）。
    found = shutil.which(BSK_BIN_NAME)
    if found:
        return (found,)
    for candidate in (Path.home() / ".local" / "bin" / "bsk.exe", Path.home() / ".local" / "bin" / "bsk"):
        if candidate.exists():
            return (str(candidate),)
    raise BskError(f"未找到 {BSK_BIN_NAME} CLI，请先安装 browser-skill（bsk）")


def _default_log(message: str) -> None:
    print(f"[bsk] {message}", file=sys.stderr)


def _js_find(selector: str | None, text: str | None, exact: bool) -> str:
    # 返回一个求值为「匹配元素数组」的 JS 表达式（内联 IIFE，bsk evaluate 只吃表达式）。
    # 无选择器时从 body 起扫（不扫 head）：<title> 的 innerText 也等于页面标题，
    # 若参与匹配，get_by_text(...).first 会落到不可见的 title 上，点击等于没点。
    # 文本过滤后只保留最深的命中节点：祖先容器 innerText 往往也等于目标文本，而点祖先
    # 不会触发挂在子节点上的处理器——与 Playwright 文本引擎一致取最深。
    return (
        "(()=>{"
        f"const sel={json.dumps(selector)};"
        f"const text={json.dumps(text)};"
        f"const exact={'true' if exact else 'false'};"
        "let els=Array.from(sel?document.querySelectorAll(sel)"
        ":(document.body||document.documentElement).querySelectorAll('*'));"
        "if(text!==null){"
        "els=els.filter(e=>{const t=(e.innerText||'').trim();return exact?t===text:t.includes(text);});"
        "els=els.filter(e=>!els.some(o=>o!==e&&e.contains(o)));"
        "}"
        "return els;"
        "})()"
    )


def _js_pick(selector: str | None, text: str | None, exact: bool, index: int) -> str:
    return f"(()=>{{const els={_js_find(selector, text, exact)};const e=els[{index}];"


class BskBrowser:
    """bsk 会话与命令执行器：一个实例对应一个 bsk session。"""

    def __init__(
        self,
        *,
        command: Sequence[str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        request_help_timeout: float | None = DEFAULT_REQUEST_HELP_TIMEOUT_SECONDS,
        slow_command_timeout: float | None = None,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self._command = tuple(command) if command is not None else resolve_bsk_command()
        self._timeout = float(timeout)
        # request_help 在扩展侧是 enabled：登录/验证码会一直阻塞等人，绝不能无限等。
        # 硬超时取 min(timeout, request_help_timeout)，先到者决定错误分类；None 表示关闭看门狗。
        self._request_help_timeout = None if request_help_timeout is None else float(request_help_timeout)
        # 慢命令（导航）单独放宽：bsk CLI 自身 --timeout 默认 30s，与看门狗同值会让
        # 正常慢加载被误判成「疑似登录阻塞」。慢命令显式传更大的 --timeout，且不再挂看门狗。
        self._slow_command_timeout = None if slow_command_timeout is None else float(slow_command_timeout)
        self._log_fn = logger or _default_log
        self._session_id: str | None = None

    # ---- 生命周期 ----

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def start(self) -> str:
        if self._session_id:
            return self._session_id
        result = self._run(
            ["session", "start", "--json", "--no-focus"],
            label="session start",
            check=False,
            stop_session_on_timeout=False,
        )
        session_id = _read_session_id(result.stdout)
        if result.returncode != 0 or not session_id:
            # 起会话失败不得遗留半开的 session：解析到 id 就补一刀 stop。
            if session_id:
                self._session_id = session_id
                self.stop()
            raise BskSessionError(f"bsk session start 失败 (exit={result.returncode})")
        self._session_id = session_id
        return session_id

    def stop(self) -> None:
        # 幂等且绝不抛：调用方常在异常路径上收尾，这里再抛会盖住原始错误。
        session_id = self._session_id
        self._session_id = None
        if not session_id:
            return
        try:
            self._run(
                ["session", "stop", session_id],
                label="session stop",
                check=False,
                stop_session_on_timeout=False,
            )
        except BskError as exc:
            self._log(f"cmd=session stop err={type(exc).__name__}")

    def __enter__(self) -> "BskBrowser":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    def page(self) -> "BskPage":
        return BskPage(self)

    # ---- 命令执行 ----

    def _run(
        self,
        args: Sequence[str],
        *,
        label: str,
        timeout: float | None = None,
        check: bool = True,
        stop_session_on_timeout: bool = True,
        help_watchdog: bool = True,
    ) -> BskResult:
        argv = [*self._command, *args]
        # 所有命令都需 BSK_AUTO_START=0：防止命令顺手拉起 daemon（daemon 由外部统一管理）。
        env = {**os.environ, "BSK_AUTO_START": "0"}
        budget = self._timeout if timeout is None else float(timeout)
        watchdog = self._request_help_timeout if help_watchdog else None
        killed_by_help_watchdog = watchdog is not None and watchdog <= budget
        deadline = min(budget, watchdog) if killed_by_help_watchdog else budget
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=deadline,
                env=env,
            )
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - started
            kind = "BskHelpTimeout" if killed_by_help_watchdog else "BskTimeout"
            self._log(f"cmd={label} exit=- dur={elapsed:.2f}s err={kind}")
            if stop_session_on_timeout:
                self.stop()
            message = (
                f"{label} 超时（>{deadline:.0f}s，疑似登录/验证码等人阻塞）"
                if killed_by_help_watchdog
                else f"{label} 超时（>{deadline:.0f}s）"
            )
            raise (BskHelpTimeout if killed_by_help_watchdog else BskTimeout)(message)
        elapsed = time.monotonic() - started
        result = BskResult(proc.returncode, proc.stdout or "", proc.stderr or "")
        code = _read_error_code(result.stdout)
        self._log(
            f"cmd={label} exit={result.returncode} dur={elapsed:.2f}s"
            + (f" err={code}" if result.returncode != 0 else "")
        )
        if result.returncode != 0 and check:
            # bsk 内部超时（退出码 4）归一到 BskTimeout，便于上层统一回退判断。
            if code == "timeout":
                raise BskTimeout(f"{label} 超时（bsk 内部超时）")
            raise BskCommandError(f"{label} 失败 (exit={result.returncode}, code={code or 'unknown'})")
        return result

    # ---- 原语 ----

    def evaluate(self, expression: str, *, timeout: float | None = None) -> Any:
        # evaluate 的 JS 异常也返回退出码 0，必须看信封里的 ok 字段。
        result = self._run(
            ["evaluate", "--json", "--session", self._require_session(), expression],
            label="evaluate",
            timeout=timeout,
        )
        payload = _read_json_object(result.stdout, "evaluate")
        if not payload.get("ok"):
            error = payload.get("error") or {}
            text = str(error.get("text") if isinstance(error, dict) else error or "")
            raise BskCommandError(f"evaluate 执行失败: {text[:200]}")
        return payload.get("value")

    def navigate(self, url: str, *, wait_until: str | None = None, timeout: float | None = None) -> None:
        # 导航是唯一真正慢的阶段（冷启动 SPA 首屏），显式给 bsk 一个远大于看门狗的 --timeout，
        # 并关掉看门狗：慢加载超时定性为普通超时，而不是「疑似登录阻塞」。
        budget = self._slow_budget(timeout)
        args = [
            "navigate",
            url,
            "--session",
            self._require_session(),
            "--timeout",
            f"{budget:.0f}s",
        ]
        if wait_until:
            args += ["--wait-until", wait_until]
        self._run(
            args,
            label=f"navigate url={url}",
            timeout=max(budget, self._timeout),
            help_watchdog=False,
        )

    def get_html(self, *, timeout: float | None = None) -> str:
        # get-html 无 --timeout 选项，只能用子进程预算兜住；同属慢命令，不挂看门狗。
        result = self._run(
            ["get-html", "--session", self._require_session()],
            label="get-html",
            timeout=max(self._slow_budget(timeout), self._timeout),
            help_watchdog=False,
        )
        return result.stdout

    def _slow_budget(self, timeout: float | None) -> float:
        if timeout is not None:
            return float(timeout)
        return self._slow_command_timeout or self._timeout

    def _require_session(self) -> str:
        if not self._session_id:
            raise BskSessionError("bsk session 尚未启动")
        return self._session_id

    def _log(self, message: str) -> None:
        self._log_fn(message)


class BskLocator:
    """最小 locator：selector（可空）+ 精确/包含文本过滤 + 序号。"""

    def __init__(
        self,
        page: "BskPage",
        selector: str | None = None,
        text: str | None = None,
        exact: bool = False,
        index: int = 0,
    ) -> None:
        self._page = page
        self._selector = selector
        self._text = text
        self._exact = exact
        self._index = index

    def _with_index(self, index: int) -> "BskLocator":
        return BskLocator(self._page, self._selector, self._text, self._exact, index)

    @property
    def first(self) -> "BskLocator":
        return self._with_index(0)

    def nth(self, index: int) -> "BskLocator":
        return self._with_index(int(index))

    def count(self) -> int:
        value = self._page.browser.evaluate(f"(()=>{{return {_js_find(self._selector, self._text, self._exact)}.length;}})()")
        return int(value or 0)

    def all_inner_texts(self) -> list[str]:
        # 一次 evaluate 取回全部命中容器的 innerText，避免逐条往返。
        value = self._page.browser.evaluate(
            f"(()=>{{return {_js_find(self._selector, self._text, self._exact)}.map(e=>e.innerText);}})()"
        )
        if not isinstance(value, list):
            return []
        return [str(item or "") for item in value]

    def inner_text(self, timeout: float | None = None) -> str:
        # ponytail: 接受但忽略 Playwright 风格 timeout（毫秒），超时统一由 BskBrowser 控制。
        # 未命中返回空串（Playwright 会抛），调用方本来就按空文本降级。
        value = self._page.browser.evaluate(
            f"{_js_pick(self._selector, self._text, self._exact, self._index)}return e?e.innerText:null;}})()"
        )
        return str(value) if value is not None else ""

    def get_by_text(self, text: str, exact: bool = False) -> "BskLocator":
        return BskLocator(self._page, self._selector, text, exact, 0)

    def click(self, timeout: float | None = None) -> None:
        # ponytail: 走 evaluate 在页内按选择器+文本定位后 e.click()，不用 observe 的 @eN 引用。
        # 理由：@eN 只在当前 DOM 快照内有效，DOM 一变即失效（实测点完一次后引用报 unknown），
        # 而「observe 拿引用→click」是两次往返且仍要处理失效重试；e.click() 一次往返、
        # 由事件冒泡触发挂在祖先行上的处理器，天然免疫引用失效。
        # 天花板：合成事件（非 CDP 真实输入），若目标站依赖 isTrusted/指针序列则需改用
        # observe + `bsk click @eN` 真实输入（升级路径）。
        # ponytail: timeout 同样被忽略，见 inner_text。
        value = self._page.browser.evaluate(
            f"{_js_pick(self._selector, self._text, self._exact, self._index)}"
            "if(!e)return 'NO_MATCH';"
            "e.scrollIntoView({block:'center'});"
            "e.click();"
            "return 'CLICKED';})()"
        )
        if value != "CLICKED":
            raise BskCommandError("click 目标未命中（DOM 已变化或选择器/文本不匹配）")


class BskPage:
    """Playwright 风格 page facade，只覆盖蓝宝书 connector 实际用到的调用面。"""

    def __init__(self, browser: BskBrowser) -> None:
        self._browser = browser

    @property
    def browser(self) -> BskBrowser:
        return self._browser

    @property
    def url(self) -> str:
        value = self._browser.evaluate("location.href")
        return str(value or "")

    def goto(self, url: str, wait_until: str | None = None, timeout: float | None = None) -> None:
        # ponytail: timeout 是 Playwright 毫秒语义，此处忽略，统一由 BskBrowser 控制。
        self._browser.navigate(url, wait_until=wait_until)

    def content(self) -> str:
        # 仅用于失败调试，token 成本高，不要在正常路径里调用。
        return self._browser.get_html()

    def close(self) -> None:
        # page 不持有独立浏览器上下文，关页不能误杀共享 session：故意空实现。
        return None

    def locator(self, selector: str) -> BskLocator:
        return BskLocator(self, selector)

    def get_by_text(self, text: str, exact: bool = False) -> BskLocator:
        return BskLocator(self, None, text, exact)


def _read_session_id(stdout: str) -> str:
    try:
        payload = json.loads(stdout or "")
    except (ValueError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("session_id") or "").strip()


def _read_error_code(stdout: str) -> str:
    try:
        payload = json.loads(stdout or "")
    except (ValueError, TypeError):
        return ""
    if isinstance(payload, dict):
        return str(payload.get("code") or "")
    return ""


def _read_json_object(stdout: str, label: str) -> dict:
    try:
        payload = json.loads(stdout or "")
    except (ValueError, TypeError):
        # 不回显 stdout：输出可能含页面内容/凭据。
        raise BskCommandError(f"{label} 输出不是合法 JSON") from None
    if not isinstance(payload, dict):
        raise BskCommandError(f"{label} 输出不是 JSON 对象")
    return payload