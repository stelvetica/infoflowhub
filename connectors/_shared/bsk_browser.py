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
# page.evaluate 可能跑 await fetch(...) 等慢表达式：默认放宽到 60s，且不挂看门狗
# （看门狗只用于识别「登录/验证码等人」阻塞，慢 evaluate 超时应定性为普通超时）。
DEFAULT_EVALUATE_TIMEOUT_SECONDS = 60.0
# wait_for_selector / locator.wait_for 默认预算（毫秒）：明显高于 30s 看门狗门限，
# 且这类调用一律关看门狗，避免正常慢等待被误分类成 BskHelpTimeout。
DEFAULT_WAIT_TIMEOUT_MS = 60_000
# 页内轮询间隔：够密以贴近 Playwright 的命中时机，又不至于空转烧 CPU。
DEFAULT_WAIT_POLL_INTERVAL_MS = 100
# 子进程预算要比页内轮询预算多留一点，避免页内 reject 之前子进程先被 kill。
WAIT_BUDGET_MARGIN_SECONDS = 10.0
BSK_BIN_NAME = "bsk"
# daemon 会拒绝 stop 一个正在执行命令的 session（退出码 4）；超时路径下 session 仍 busy，
# 因此 stop 失败后等一小会儿再试一次，避免 daemon 侧 session 泄漏。
STOP_RETRY_DELAY_SECONDS = 1.5


class BskError(Exception):
    """bsk 适配层错误基类。"""


class BskTimeout(BskError):
    """子进程硬超时，或 bsk 自身报告超时（退出码 4 / code=timeout）。"""


class BskHelpTimeout(BskTimeout):
    """命令被"等待看门狗"先判定超时（症状是长时间无返回），上层应判定本次抓取失败。

    成因不止一种，别断言：扩展的「允许请求人工协助」开启时可能是登录/验证码在等人，
    关闭时则更可能是页面卡住或网络慢。文案只说症状，不猜原因。
    """


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


def _js_call(js: str, arg: Any) -> str:
    # page.evaluate(js, arg) 的传参约定（对齐 Playwright）：js 是「函数表达式」时，
    # 构造成 `(js)(<json arg>)` 由 bsk 调用；arg 为 None 时按普通表达式求值（js 原样下发）。
    # arg 用 json.dumps 默认 ensure_ascii=True，非 ASCII 转义成 \uXXXX，规避 Windows
    # 命令行编码问题（与 _js_find 里 json.dumps 的处理一致）。
    if arg is None:
        return js
    return f"({js})({json.dumps(arg)})"


def _js_wait(selector: str | None, text: str | None, exact: bool, state: str, timeout_ms: int, interval_ms: int) -> str:
    # bsk 没有原生等待命令：在页内跑一个轮询 Promise，命中即 resolve，超时 reject。
    # 一次 evaluate 内完成轮询，避免多次往返；bsk evaluate 默认 await-promise=true 会等它。
    # state 语义：visible（默认）要求元素有非零尺寸且 visibility!=hidden；其余一律按
    # attached（只要存在）处理，见 _normalize_wait_state 的取舍说明。
    attached_only = "false" if state == "visible" else "true"
    return (
        "(()=>{"
        f"const deadline=Date.now()+{int(timeout_ms)};"
        "return new Promise((resolve,reject)=>{"
        "const check=()=>{"
        f"const els={_js_find(selector, text, exact)};"
        "const el=els[0];"
        f"const attachedOnly={attached_only};"
        "let ok=false;"
        "if(el){"
        "if(attachedOnly){ok=true;}"
        "else{const r=el.getBoundingClientRect();"
        "ok=r.width+r.height>0&&getComputedStyle(el).visibility!=='hidden';}"
        "}"
        "if(ok){resolve(true);return;}"
        "if(Date.now()>=deadline){"
        f"reject(new Error('等待选择器超时（{int(timeout_ms)}ms 内未满足 {state}）'));return;}}"
        f"setTimeout(check,{int(interval_ms)});"
        "};"
        "check();"
        "});"
        "})()"
    )


def _normalize_wait_state(state: str | None) -> str:
    # 只区分 visible / attached 两态：Playwright 的 hidden/detached 等本层不实现，
    # 一律降级为 attached（「元素存在即可」），宁可放行也不误伤正常路径。
    return "visible" if state == "visible" else "attached"


def _to_int(value: Any) -> int:
    # 非数字按 0；保留正负号（滚轮 delta 可为负，表示反向）。
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _clamp_wait_ms(value: Any) -> int:
    # wait_for_timeout 的入参容错：非数字按 0（不调用），负数钳到 0。
    return max(_to_int(value), 0)


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
        # 看门狗（等待看门狗）：命令长时间无返回时先于硬超时判定失败，绝不能无限等。
        # 成因不预设——扩展的「允许请求人工协助」开着时可能是登录/验证码在等人，关着时
        # 更可能是页面卡住或网络慢，故只按症状判定，不据此断言原因。
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
        started_at_ms = int(time.time() * 1000)
        try:
            # start 阶段尚未打开任何页面，不存在「登录/验证码等人」阻塞，故不挂看门狗：
            # 否则 start 超时会被误分类成 BskHelpTimeout，把排障指向错误方向。
            result = self._run(
                ["session", "start", "--json", "--no-focus"],
                label="session start",
                check=False,
                stop_session_on_timeout=False,
                help_watchdog=False,
            )
        except BskTimeout:
            # 超时被 kill 时拿不到 session_id，但 daemon 可能已建好 session（半开泄漏）：
            # 按「本次 start 之后新建」清掉孤儿，客户端拿不到 id 也要能收尾。
            self._cleanup_orphan_sessions(started_at_ms)
            raise
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
        if not self._stop_session_with_retry(session_id):
            self._log("cmd=session stop err=retry-failed")

    def _stop_session_with_retry(self, session_id: str) -> bool:
        # 命令超时路径下 session 仍 busy，首次 stop 常被 daemon 拒绝（退出码 4），
        # 故失败后延迟重试一次；仍失败由调用方记录，不抛。
        if self._stop_session(session_id):
            return True
        time.sleep(STOP_RETRY_DELAY_SECONDS)
        return self._stop_session(session_id)

    def _stop_session(self, session_id: str) -> bool:
        """停指定 session；返回是否确认成功（绝不抛）。"""
        try:
            result = self._run(
                ["session", "stop", session_id],
                label="session stop",
                timeout=self._control_timeout(),
                check=False,
                stop_session_on_timeout=False,
                # 收尾命令与「登录/验证码等人」无关，不能挂看门狗（否则极小配置下会被
                # 看门狗提前 kill，收尾永远失败）。
                help_watchdog=False,
            )
        except BskError as exc:
            self._log(f"cmd=session stop err={type(exc).__name__}")
            return False
        return result.returncode == 0

    def _cleanup_orphan_sessions(self, started_at_ms: int) -> None:
        """清掉「本次 start 之后新建、且不属于我们」的孤儿 session（拿不到 id 时的兜底）。"""
        try:
            result = self._run(
                ["session", "list", "--json"],
                label="session list",
                timeout=self._control_timeout(),
                check=False,
                stop_session_on_timeout=False,
                help_watchdog=False,
            )
        except BskError as exc:
            self._log(f"cmd=session list err={type(exc).__name__}")
            return
        for session_id in _read_orphan_session_ids(result.stdout, started_at_ms):
            if session_id == self._session_id:
                continue
            if not self._stop_session_with_retry(session_id):
                self._log(f"cmd=session stop session={session_id} err=retry-failed")

    def _control_timeout(self) -> float:
        # stop/list 是收尾用的快命令，不能沿用调用方可能极小的业务预算（否则收尾本身超时，
        # session 照样泄漏）；给一个不小于默认值的下限。
        return max(self._timeout, DEFAULT_TIMEOUT_SECONDS)

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
                f"{label} 超时（>{deadline:.0f}s 无返回，页面可能卡住或停在需人工处理的步骤）"
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

    def evaluate(self, expression: str, *, timeout: float | None = None, help_watchdog: bool = True) -> Any:
        # evaluate 的 JS 异常也返回退出码 0，必须看信封里的 ok 字段。
        # 显式把预算透传给 bsk --timeout：bsk 自身默认 30s，否则页内长轮询/await fetch
        # 会被 bsk 先判超时（退出码 4），调用方传更大 timeout 也无效。
        budget = self._timeout if timeout is None else float(timeout)
        result = self._run(
            [
                "evaluate",
                "--json",
                "--session",
                self._require_session(),
                "--timeout",
                f"{budget:.0f}s",
                expression,
            ],
            label="evaluate",
            timeout=budget,
            help_watchdog=help_watchdog,
        )
        payload = _read_json_object(result.stdout, "evaluate")
        if not payload.get("ok"):
            error = payload.get("error") or {}
            text = str(error.get("text") if isinstance(error, dict) else error or "")
            raise BskCommandError(f"evaluate 执行失败: {text[:200]}")
        return payload.get("value")

    def wait_ms(self, duration_ms: Any, *, timeout: float | None = None) -> None:
        # 浏览器侧等待（bsk wait-ms 在 daemon 侧 sleep），不用本地 time.sleep。
        # wait-ms 无需 --session，也不接受 --timeout，只能用子进程预算兜住。
        ms = _clamp_wait_ms(duration_ms)
        if ms <= 0:
            return
        budget = max(self._timeout, ms / 1000.0 + WAIT_BUDGET_MARGIN_SECONDS)
        self._run(
            ["wait-ms", f"{ms}ms"],
            label=f"wait-ms dur={ms}ms",
            timeout=budget,
            help_watchdog=False,
        )

    def wheel(self, delta_x: Any, delta_y: Any, *, selector: str | None = None, timeout: float | None = None) -> None:
        # bsk wheel 要求至少一个非零 delta，双零直接当无操作返回（不产生空命令）。
        dx = _to_int(delta_x)
        dy = _to_int(delta_y)
        if dx == 0 and dy == 0:
            return
        budget = self._timeout if timeout is None else float(timeout)
        args = [
            "wheel",
            "--session",
            self._require_session(),
            "--delta-x",
            str(dx),
            "--delta-y",
            str(dy),
            "--timeout",
            f"{budget:.0f}s",
        ]
        if selector:
            args += ["--selector", selector]
        self._run(args, label="wheel", timeout=budget, help_watchdog=False)

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

    def inner_html(self) -> str:
        # 与 Playwright Locator.inner_html() 同形：取命中元素的内部 HTML（结构化解析用）。
        value = self._page.browser.evaluate(
            f"{_js_pick(self._selector, self._text, self._exact, self._index)}return e?e.innerHTML:null;}})()"
        )
        return str(value) if value is not None else ""

    def inner_text(self, timeout: float | None = None) -> str:
        # ponytail: 接受但忽略 Playwright 风格 timeout（毫秒），超时统一由 BskBrowser 控制。
        # 未命中返回空串（Playwright 会抛），调用方本来就按空文本降级。
        value = self._page.browser.evaluate(
            f"{_js_pick(self._selector, self._text, self._exact, self._index)}return e?e.innerText:null;}})()"
        )
        return str(value) if value is not None else ""

    def get_by_text(self, text: str, exact: bool = False) -> "BskLocator":
        return BskLocator(self._page, self._selector, text, exact, 0)

    def evaluate_all(self, expression: str, arg: Any = None, *, timeout: float | None = DEFAULT_EVALUATE_TIMEOUT_SECONDS) -> Any:
        # Playwright 语义：expression 收到「全部命中元素数组」作为第一入参、arg 作为第二入参，
        # 返回其返回值（详见 playwright async_api 的 evaluate_all 文档）。故连接器里写成
        # `els => {...for (const el of els)...}` 是按数组遍历，本层必须传数组而非单元素。
        # 一次 evaluate 内用 querySelectorAll 取全量，无命中即传空数组（expression 通常返回 []）。
        # 非 ASCII 由 json.dumps 默认转义，规避 Windows 命令行编码问题。
        call = f"((els,arg)=>(({expression})(els,arg)))({_js_find(self._selector, self._text, self._exact)}, {json.dumps(arg)})"
        return self._page.browser.evaluate(call, timeout=timeout, help_watchdog=False)

    def wait_for(
        self,
        timeout: float | None = DEFAULT_WAIT_TIMEOUT_MS,
        state: str = "visible",
    ) -> None:
        # 与 page.wait_for_selector 共用页内轮询实现；命中即返回，超时抛 BskCommandError。
        self._page._wait_for(self._selector, self._text, self._exact, timeout, state)

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


class BskMouse:
    """Playwright 风格 page.mouse facade：目前只覆盖滚轮。"""

    def __init__(self, page: "BskPage") -> None:
        self._page = page

    def wheel(self, delta_x: float, delta_y: float, *, selector: str | None = None, timeout: float | None = None) -> None:
        # dx 为 0 时只下发 --delta-y（bsk 只要求至少一个非零），dy 同理。
        self._page.browser.wheel(delta_x, delta_y, selector=selector, timeout=timeout)


class BskPage:
    """Playwright 风格 page facade，只覆盖蓝宝书 connector 实际用到的调用面。"""

    def __init__(self, browser: BskBrowser) -> None:
        self._browser = browser

    @property
    def browser(self) -> BskBrowser:
        return self._browser

    @property
    def mouse(self) -> BskMouse:
        return BskMouse(self)

    @property
    def url(self) -> str:
        value = self._browser.evaluate("location.href")
        return str(value or "")

    def evaluate(self, js: str, arg: Any = None, *, timeout: float | None = DEFAULT_EVALUATE_TIMEOUT_SECONDS) -> Any:
        # 同形 page.evaluate(js[, arg])：js 为函数表达式时按 (js)(arg) 调用（见 _js_call）。
        # 默认预算 60s 且关看门狗：await fetch(...) 这类慢表达式不应被 30s 看门狗误判成
        # 「疑似登录阻塞」（BskHelpTimeout）。bsk evaluate 的 --await-promise 保持默认 true。
        return self._browser.evaluate(_js_call(js, arg), timeout=timeout, help_watchdog=False)

    def wait_for_timeout(self, ms: Any) -> None:
        # 走浏览器侧 bsk wait-ms（不用本地 sleep）；负数/非数字被钳成 0 即不调用。
        self._browser.wait_ms(ms)

    def wait_for_selector(
        self,
        selector: str,
        timeout: float | None = DEFAULT_WAIT_TIMEOUT_MS,
        state: str = "visible",
    ) -> None:
        self._wait_for(selector, None, False, timeout, state)

    def _wait_for(
        self,
        selector: str | None,
        text: str | None,
        exact: bool,
        timeout: float | None,
        state: str,
    ) -> None:
        # 页内轮询 Promise，超时 reject → bsk evaluate 返回 ok=false → 抛 BskCommandError
        # （与 Playwright TimeoutError 语义对齐，文案说明是等待选择器超时）。
        ms = DEFAULT_WAIT_TIMEOUT_MS if timeout is None else int(timeout)
        normalized = _normalize_wait_state(state)
        budget = ms / 1000.0 + WAIT_BUDGET_MARGIN_SECONDS
        expr = _js_wait(selector, text, exact, normalized, ms, DEFAULT_WAIT_POLL_INTERVAL_MS)
        self._browser.evaluate(expr, timeout=budget, help_watchdog=False)

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


def _read_orphan_session_ids(stdout: str, started_at_ms: int) -> list[str]:
    """从 `session list --json` 输出里挑出「本次 start 之后新建」的 session id。"""
    try:
        payload = json.loads(stdout or "")
    except (ValueError, TypeError):
        return []
    if not isinstance(payload, list):
        return []
    ids: list[str] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        session_id = str(item.get("session_id") or "").strip()
        created_at = item.get("created_at_ms")
        if not session_id or not isinstance(created_at, (int, float)):
            continue
        if int(created_at) >= started_at_ms:
            ids.append(session_id)
    return ids


def _read_json_object(stdout: str, label: str) -> dict:
    try:
        payload = json.loads(stdout or "")
    except (ValueError, TypeError):
        # 不回显 stdout：输出可能含页面内容/凭据。
        raise BskCommandError(f"{label} 输出不是合法 JSON") from None
    if not isinstance(payload, dict):
        raise BskCommandError(f"{label} 输出不是 JSON 对象")
    return payload