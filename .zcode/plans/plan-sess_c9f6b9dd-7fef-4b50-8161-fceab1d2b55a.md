## 目标

B站 cookie 从"手动填 .env"升级为"复用复制常用Chrome机制自动导出",对标蓝宝书的 copy_profile + 微信的持久化模式。本机 Chrome 已登录 B站(32条cookie,含SESSDATA/bili_jct/DedeUserID),无需重新扫码。

## 改动清单(5个文件)

### 1. `connectors/auth/providers/bilibili.py` — 核心改造

新增持久化 + 导出逻辑,对标 `wechat.py` 结构:

- 新增常量 `BILIBILI_AUTH_PATH = AUTH_DIR / "bilibili_main.json"`(与 `WECHAT_AUTH_PATH` 同目录)
- 新增 `export_bilibili_cookie_from_chrome() -> str`:用 `CopyRunnerSession` 复制本机 Chrome Default profile 到 `runtime/browser_profiles/bilibili-runner/`,Playwright 打开 `https://www.bilibili.com/` 后 `context.cookies()` 过滤 `.bilibili.com` 域,拼接成 `SESSDATA=x; bili_jct=y; DedeUserID=z` 格式,存入 json 文件。复用蓝宝书 runner 的关Chrome→复制→抓取→重开Chrome 全流程
- 改 `get_bilibili_cookie() -> str`:优先读 `bilibili_main.json` 的 cookie 字段;读不到回退现有 .env 逻辑(向后兼容)
- 改 `validate_bilibili_auth()`:读 json 文件的 cookie;可选调一次 nav 接口判 isLogin 做过期检测(对标微信的 is_expired)

### 2. `web/services/views.py` — 设置界面按钮

在 auth_assets 渲染中(816-857行),给 `bilibili_main` 加 `renew_action_url="/actions/bilibili-login/sync"`、`renew_action_label="同步登录态"`,让设置界面出现按钮(复用微信已有的 renew_action 机制)。

### 3. `web/routes/main.py` — 新增路由

新增 `POST /actions/bilibili-login/sync`:调 `export_bilibili_cookie_from_chrome()`,成功后返回 auth_assets_panel 片段(对标 `/actions/wechat-login/renew` 的写法)。

### 4. `connectors/auth/providers/__init__.py` — 导出新函数

导出 `export_bilibili_cookie_from_chrome`、`BILIBILI_AUTH_PATH`。

### 5. `connectors/auth/registry.py` — 更新资产描述

更新 `bilibili_main` 的 `login_method`、`description`、`renew_label`,反映新机制(从"手动Cookie(.env)"改为"复制常用Chrome导出")。

## 不改动

- `connectors/bilibili/api.py`:仍经 `get_bilibili_cookie()` 取 cookie,SSOT 不变,自动吃到新数据源
- `.env` 里的 `BILIBILI_COOKIE`:保留作为回退兜底
- 蓝宝书 runner 代码:`CopyRunnerSession` 直接复用,不改动

## 设计要点

- **关Chrome问题**:导出需关本机 Chrome 复制 profile(蓝宝书同款限制)。CopyRunnerSession.shutdown 已会自动重开 Chrome。设置界面按钮点击时用户在用浏览器,但 htmx 请求是后台执行,关→复制→读cookie→重开 全程几秒,可接受
- **过期检测**:B站 cookie 无明确过期时间字段,靠 nav 接口 `code=-101` 判定。validate 时调一次 nav,isLogin=False 则标记 is_expired
- **代理问题**:前一轮发现的 ProxyError 根因不在本方案范围(那是 BilibiliApiSession 读系统代理被污染),本方案只解决"cookie 获取"环节。代理修复可后续单独处理

## 验证

- 跑 `export_bilibili_cookie_from_chrome()` 实测能读出有效 cookie(SESSDATA 等)
- `get_bilibili_cookie()` 优先读 json 成功
- `validate_bilibili_auth()` 返回可用状态
- 设置界面 B站行出现"同步登录态"按钮