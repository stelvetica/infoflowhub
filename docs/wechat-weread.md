# 微信公众号抓取：外部工具微信读书通道（唯一通道）

## 背景

微信于 2026-07-30 关闭了公众号后台文章列表接口 `mp.weixin.qq.com/cgi-bin/appmsgpublish`
（对一切请求统一返回 `200013 freq control`），旧通道（公众号后台 token+cookie 直连）
已从本仓代码中删除，不可恢复。

现行唯一通道：**外部工具 `wechat-mp-tools`（微信读书模式）**。
本仓 9 个公众号源配置为 `weread://<fakeid>@127.0.0.1:5300`，
抓取时由 `connectors/wechat/feed.py` 调工具的 `GET /api/articles/list/<fakeid>`
直取文章元数据（标题/链接/摘要/时间），不走 RSS 文件。

> ⚠️ 该工具依赖微信读书的非官方接口，且请求会经第三方转发服务（weread 中转域）。
> 微信读书会话会交给匿名第三方；且**并非所有公众号都被微信读书收录**——未收录的号无法通过此通道抓取。

## 外部工具与启动

工具已在 `wechat-mp-tools/`（含独立 venv312）。`scripts/start_infoflow_web.ps1`
的 Phase 1.5 会在 5300 端口未监听时自动拉起：

```powershell
wechat-mp-tools\venv312\Scripts\python.exe app.py --port 5300 --no-browser
# 日志：runtime\mp_tools.log
```

手动启动也用同一命令（注意 `--port 5300`，工具默认 5200 与配置不符）。
外部工具地址 SSOT：`config/rss_settings.json -> wechat_tool.base_url`。

## 登录（用户人工，约 3~4 天失效一次）

1. 打开主仓界面 →「微信续期」（或设置页「认证资产」→ 微信公众号「重新扫码」）。
2. 用**手机微信扫码登录微信读书**。⚠️ 勿勾选「24 小时后自动退出」。
3. 账号池里的账号 Token 失效（401）后工具自动标记 invalid，需重新扫码。

## 添加公众号

1. 找到该公众号任一篇文章链接（`https://mp.weixin.qq.com/s/...`）。
2. 在工具里粘贴链接，自动反查 fakeid 并添加订阅。
3. 在本仓 `config/subscription_sources.json` 增加 `weread://<fakeid>@127.0.0.1:5300` 源。

## 验证

```bash
curl http://127.0.0.1:5300/api/auth/status          # logged_in 应为 true
curl http://127.0.0.1:5300/api/articles/list/<fakeid>?count=12
```

工具未运行时，主仓设置页显示「外部工具未运行」；登录过期时显示需重新扫码，
对应源抓取报错入库，不回退任何旧通道。
