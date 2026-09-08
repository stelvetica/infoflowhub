# Firefox 备用方案

> 状态：**备用，未接入管线**。2026-09-08 实测验证。当 Chromium/CDP 路线整体失效（Google 风控升级、Chrome 反自动化收紧）时的第三退路。前置退路见 `docs/bb-browser-fallback.md`。

## 为什么 Firefox 是有效退路

1. **无 Google 账号-浏览器绑定体系**：没有 ABE、没有 Secure Preferences、没有"副本 Chrome 以已登录身份向 Google 发起账号校验"这回事——困扰 Chrome 路线的"验证登录"问题在 Firefox 上不存在。
2. **cookie 明文可读**：`cookies.sqlite` 是普通 sqlite，无需 os_crypt/DPAPI 解密，复制注入比 Chrome 简单得多。
3. **Playwright 原生支持**：`p.firefox` 三引擎之一，无需 CDP。

## 实测结论（2026-09-08，本机）

| 项 | 结果 |
|---|---|
| Firefox Developer Edition 156 直连 Playwright | ❌ **连不上**（`launch_persistent_context` 连干净 profile 都超时）——Dev 通道版本太新，Playwright 的 juggler 补丁未跟进 |
| Playwright 自带内核（`p.firefox.launch()`，firefox-1522 / FF 150） | ✅ 正常：导航、取标题、执行 JS、注入 cookie 全通 |
| 从 Firefox profile 读 cookie 注入干净实例 | ✅ B 站实测 `isLogin: True`（SESSDATA/bili_jct/DedeUserID 三件套完整带出） |
| 挂真实 profile 副本（`launch_persistent_context` 指向复制的 profile） | ❌ 超时——profile 内扩展/会话恢复卡启动，**此路不通**，勿再试 |

**结论：Firefox 的正确用法是"读 cookie → 注入 Playwright 自带内核"，不是驱动本机安装的 Firefox 本人。**

## 本机 profile 实况

- `mdtmxssr.dev-edition-default`：Firefox Dev 日常 profile，**登录态在此**（B站、蓝宝书 rabyte.cn、Google）
- `dvzii2le.default-release`：无 B 站（Google/YouTube/插件）
- `vbugmozx.default`：空壳
- 路径规律：`%APPDATA%\Mozilla\Firefox\Profiles\<name>\cookies.sqlite`，表 `moz_cookies(host, name, value, path, isSecure, isHttpOnly, expiry)`

## 可复用的登录态读取注入代码（实测通过）

```python
import sqlite3, shutil, tempfile, os
from playwright.sync_api import sync_playwright

COOKIES_DB = r"<profile目录>\cookies.sqlite"

def load_firefox_cookies(db_path, host_filter="%bilibili%"):
    tmp = tempfile.mktemp(suffix=".sqlite")
    shutil.copy2(db_path, tmp)          # 原文件可能被 Firefox 持锁，先拷走
    conn = sqlite3.connect(tmp)
    rows = conn.execute(
        "select host, name, value, path, isSecure, isHttpOnly "
        "from moz_cookies where host like ?", (host_filter,)
    ).fetchall()
    conn.close(); os.unlink(tmp)
    return rows

rows = load_firefox_cookies(COOKIES_DB)
with sync_playwright() as p:
    browser = p.firefox.launch(headless=True)
    ctx = browser.new_context()
    for host, name, value, path, secure, httponly in rows:
        if value:
            ctx.add_cookies([{"name": name, "value": value, "domain": host,
                              "path": path or "/", "secure": bool(secure),
                              "httpOnly": bool(httponly)}])
    page = ctx.new_page()
    page.goto("https://api.bilibili.com/x/web-interface/nav")
    # 验证: json.loads(page.evaluate("document.body.innerText"))["data"]["isLogin"]
    browser.close()
```

## 接入管线的改造点（若启用）

1. `chrome_runner.py` 的复制/剥离逻辑不适用——Firefox 无 profile 复制需求，直接读 cookies.sqlite 注入。
2. 各 connector 的 `CopyRunnerSession`/`SharedRunnerSession` 换成 Firefox context（Playwright `p.firefox`），页面解析逻辑（DOM 选择器）无需大改。
3. cookie 过期后需在 Firefox 里重登一次，再跑一次注入（可写自动化脚本定时同步）。

## 待验证（装普通版 Firefox 后）

- [ ] Playwright `p.firefox.launch(executable_path=<正式版>)` 能否直接驱动（**重要**：仍建议用 Playwright 自带内核——正式版同样可能因版本过新连不上，自带的永远匹配）
- [ ] 正式版与 Dev 版 profile 是否共用（不共用：正式版 profile 在 `%APPDATA%\Mozilla\Firefox\Profiles\` 下新建，登录态需重新登录一次）
