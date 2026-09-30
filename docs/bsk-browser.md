# bsk（BrowserSkill）浏览器抓取

> 2026-09-24：**源表驱动的订阅流**已全部迁到 bsk。2026-09-28：**稍后处理（laterhub）的两个收藏源**（抖音收藏、小黑盒收藏）也已迁到 bsk，复用日常 Chrome 登录态；B 站稍后看不进 bsk——它走官方 API 直连（HTTP + `.env` cookie）更快更稳，且不依赖浏览器在场。旧「复制 profile」路径降为设置页按源显式触发的最后手段（见文末）。

## 一句话定位

bsk 是一个通过浏览器扩展驱动**本机日常 Chrome** 的抓取工具：管线默认后端即 bsk，抓取时直接复用你日常 Chrome 里已登录的会话，不复制 profile、不导出 token。少数源若需回退，只能在设置页源表**按源显式**用「copy 重试」——它是最后手段，会复制 profile 并可能让你退出 Google 账号。

## 稍后处理（laterhub）三源

- 抖音收藏、小黑盒收藏已从「复制 Chrome profile」迁到 bsk，复用日常 Chrome 实时登录会话；B 站稍后看不走 bsk。
- 两个 bsk 源共用一个 bsk 会话；某源超时只停该 bsk 会话，**不会关闭或重开你的日常 Chrome**。
- 小黑盒收藏：bsk 无请求/响应拦截能力，改为在页内借前端 `$hbRequest` 拿签名后直接取数（不再用 `page.on("request")`）。
- B 站稍后看：官方 API 直连（`x/v2/history/toview/web`，`Referer` 为 watchlater 页）。登录态按 SSOT 取本地认证文件 `runtime/auth/bilibili_main.json`（设置页「同步登录态」维护），回退 `.env` 的 `BILIBILI_COOKIE`/`BILIBILI_SESSDATA`；报 `-101 账号未登录` 时去设置页点一次「同步登录态」。不启浏览器、不用 session——bsk 会话起不来时它照样能抓。

## 版本与更新

- **CLI / daemon**：`bsk update` 升级，`bsk update --check` 只检查不升级。
- **浏览器扩展**：走浏览器扩展自己的更新渠道，`bsk update` **无法更新扩展**。
- **扩展升级后 CLI 会自动跟上**：上游把 CLI 与扩展**成对发布**、协议需一致，而扩展会在 Chrome 里静默升级、CLI 不会自己动。故 `check_bsk_update.ps1` 检测到**扩展版本高于 CLI** 时直接 `bsk update -y` 把 CLI 追平（这是该脚本唯一的写动作，只在真需要时发生）。反方向无解——CLI 更新不了扩展，只能等扩展自己升，脚本只提示。
- **三者协议需一致**：`bsk status` 报 `daemon_version`、每个已连接浏览器的 `extension_version`，并用 `version_skew_browsers` 列出错配的浏览器。当前基线：CLI/daemon `0.3.2`、扩展 `0.3.2`、协议双方 `1.3`，无错配。

## 运行前提：权限级别必须与 daemon 一致

daemon 由「InfoFlowHub 开机自启」计划任务以 **Highest（提权）** 启动，它创建的命名管道
**拒绝低完整性（LIMITED / 非提权）客户端的连接**——实测报 `os error 5 拒绝访问`：管道存在、
52800 端口可达，纯粹是权限问题，很容易被误读成「daemon 没在跑」。

因此：**凡是需要与 daemon 通信的进程（bsk 命令行、抓取用的 web 服务、bsk 更新检查）
都必须与 daemon 处于同一权限级别**。实践含义：

- bsk 更新检查跑在 web 服务进程里（见「定期检查」），权限随 web 服务——它由提权任务拉起，故一致；单独在非提权终端手动跑该脚本会误报 daemon 不可达。
- 重启/手动拉起 web 服务时，若 daemon 是提权起的，web 服务也需提权，否则 bsk 抓取会失败。

## 定期检查

- web 服务**每天**跑一次 `scripts/check_bsk_update.ps1`（`auto_runner._maybe_check_bsk_update()`，戳文件 `runtime/bsk_update_last_check.txt`），结果追加到 `runtime/bsk_update_check.log`（同时打印到控制台）。按天而非原先计划任务的每 14 天：扩展随时可能静默升级，粒度太粗会让「扩展升级 → CLI 追平」滞后到两周。
- 退出码：`0` 全部最新；`2` 仍有待处理项（有更新可用、存在版本错配、或扩展仍领先 CLI）；`1` 检查本身失败（如 bsk 不在 PATH）。拿不到 daemon 状态时脚本优雅降级（仍报 CLI 结果并**如实写明失败原因**，便于区分「没在跑」与「权限不足」），不会整体失败。
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