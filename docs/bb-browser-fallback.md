# bb-browser 备用方案（暂存）

> 状态：**已卸载，仅存档**。2026-09-08 试用验证后封存，2026-09-22 卸载 `bb-browser` npm 包并删除 `~/.bb-browser`（637MB）。浏览器抓取已全部改走 bsk，copy（复制 profile）后端也已整体删除，本文档的重启条件因此成立；重启需先按下文重装。

## 是什么

[bb-browser](https://github.com/epiral/bb-browser)（6.2k stars，MIT，npm 分发）：CLI/MCP 工具，让 AI Agent 用你浏览器里的登录态调网站接口。社区适配器覆盖 B站、搜狗微信、YouTube、雪球、Twitter 等 36 平台 103 命令。

## 为什么暂存（核心结论）

bb **绕不开**复制管线，也就绕不开 Google 账号验证问题：

- infoflowhub 至少有一个源（**蓝宝书/Alpha派**）是**单设备登录**——登录态只存在于常用 Chrome，bb 浏览器里登录会互踢，所以蓝宝书必须走 `CopyRunnerSession` 复制管线（`connectors/alphapai/runner.py:29`）。
- 只要复制管线还在，每 7 天一次的副本重建就会强杀 Chrome + 触碰 Google 相关 profile 数据，账号验证弹窗的诱因就还在。
- bb 只能接管多设备登录站点（B站/YouTube/小黑盒），这些站点现有管线本来就工作正常——bb 解决的是不存在的问题。

## 已验证的事实（2026-09-08 试用记录）

- v0.14.2 安装：`npm install -g bb-browser`，`bb-browser daemon start/stop` 管理。
- bb **不能接管日常 Chrome**：Chrome 152+ 出于安全禁止在默认 User Data 目录开 `--remote-debugging-port`（实测 9222 起不来），bb 只能用自带独立 profile（`~/.bb-browser/browser/user-data`）。
- 登录态模型：在 bb 浏览器里登录一次 → cookie 持久存其 profile → `site <平台>/<命令>` 持续可用，与日常 Chrome 隔离、无风控双活。
- 实测可用：`open`/`snap`/`screenshot`/`cookies`/`eval` 正常；`site bilibili/me` 在未登录时报"账号未登录"（符合预期）。
- daemon 固定连 `127.0.0.1:9222`（`~/.bb-browser/daemon.json` 可改端口）。

## 重启条件与步骤

满足任一条件可重启评估：

1. 蓝宝书放弃单设备限制，或找到多设备可用的替代源 → 复制管线可整体退役。
2. Google 风控升级，复制副本连"零账号身份"也被挑战。
3. 现有 Chromium/Playwright 管线大面积失效，需要快速兜底。

重启步骤：

```bash
npm install -g bb-browser          # 安装/更新
bb-browser daemon start            # 起 daemon（自带浏览器）
bb-browser open <登录页URL>         # 手动登录需要的站点（多设备站点）
bb-browser site list               # 看可用适配器
bb-browser site bilibili/feed      # 示例：带登录态抓 B 站动态
bb-browser guide                   # 新站点适配指南（AI 可自动逆向）
```

与现有管线的关系：并行不冲突——bb 用自己的 profile，复制管线用 `runtime/browser_profiles/*-runner`，日常 Chrome 两边都不碰。可先作为人工救急工具（如公众号账号池失效时的微信搜索备胎），再考虑接入 auto_runner。

## 相关背景

- 谷歌验证登录问题根因与修复史：`connectors/_shared/chrome_runner.py` 内 `_strip_google_identity` / `_strip_google_cookies` 注释，及提交 6dc8804 → a1f385c → 74b11ff → 432259e。
- ABE 已通过注册表策略关闭（README「Chrome ABE」节）。
