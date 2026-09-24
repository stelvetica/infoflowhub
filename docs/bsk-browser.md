# bsk（BrowserSkill）浏览器抓取

> 2026-09-24：浏览器抓取已全部迁到 bsk，复用日常 Chrome 登录态。旧「复制 profile」路径降为设置页按源显式触发的最后手段（见文末）。

## 一句话定位

bsk 是一个通过浏览器扩展驱动**本机日常 Chrome** 的抓取工具：管线默认后端即 bsk，抓取时直接复用你日常 Chrome 里已登录的会话，不复制 profile、不导出 token。少数源若需回退，只能在设置页源表**按源显式**用「copy 重试」——它是最后手段，会复制 profile 并可能让你退出 Google 账号。

## 版本与更新

- **CLI / daemon**：`bsk update` 升级，`bsk update --check` 只检查不升级。
- **浏览器扩展**：走浏览器扩展自己的更新渠道，`bsk update` **无法更新扩展**；扩展版本错配只能手动更新扩展。
- **三者协议需一致**：`bsk status` 报 `daemon_version`、每个已连接浏览器的 `extension_version`，并用 `version_skew_browsers` 列出错配的浏览器。当前基线：CLI/daemon `0.3.1`、扩展 `0.3.0`、协议双方 `1.3`，无错配。

## 运行前提：权限级别必须与 daemon 一致

daemon 由「InfoFlowHub 开机自启」计划任务以 **Highest（提权）** 启动，它创建的命名管道
**拒绝低完整性（LIMITED / 非提权）客户端的连接**——实测报 `os error 5 拒绝访问`：管道存在、
52800 端口可达，纯粹是权限问题，很容易被误读成「daemon 没在跑」。

因此：**凡是需要与 daemon 通信的进程（bsk 命令行、抓取用的 web 服务、本更新检查任务）
都必须与 daemon 处于同一权限级别**。实践含义：

- 更新检查任务注册时必须 `/RL HIGHEST`（用 LIMITED 会稳定误报 daemon 不可达）。
- 重启/手动拉起 web 服务时，若 daemon 是提权起的，web 服务也需提权，否则 bsk 抓取会失败。

## 定期检查

- 计划任务「bsk 更新检查（每14天）」每 14 天运行 `scripts/check_bsk_update.ps1`，结果追加到 `runtime/bsk_update_check.log`（同时打印到控制台）。
- 退出码：`0` 全部最新；`2` 有更新可用或 CLI/扩展版本错配；`1` 检查本身失败（如 bsk 不在 PATH）。拿不到 daemon 状态时脚本优雅降级（仍报 CLI 结果并**如实写明失败原因**，便于区分「没在跑」与「权限不足」），不会整体失败。
- 注册脚本 `scripts/register_bsk_update_task.ps1`（以当前用户、**提权**、隐藏窗口运行）。
- 手动检查：
  - `bsk update --check --json` 看 CLI 是否有更新
  - `bsk status --json` 看 daemon 与扩展版本、是否错配

## 抓取出错时的排查顺序

1. 先 `bsk update --check`：CLI/扩展版本错配是常见故障源，有更新就先更新 CLI（扩展另走扩展渠道）。
2. `bsk status`：确认 daemon 在跑、扩展已连接（看 `browsers` 列表与 `version_skew_browsers`）。
   若报 `os error 5 拒绝访问`，**不是 daemon 没跑，而是调用方权限级别低于 daemon**（见上文「运行前提」）。
3. 确认 Chrome 开着且扩展在线（扩展离线时 daemon 收不到浏览器会话）。
4. 看源表「最近错误」：判断是否某源登录态失效，需在**日常 Chrome 重新登录**该站点。
5. 以上都排除后，才考虑设置页「用 copy 重试」——**最后手段**，它会复制 profile 并可能登出 Google 账号。