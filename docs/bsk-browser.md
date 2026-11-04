# bsk（BrowserSkill）浏览器抓取

> 2026-09-24：**源表驱动的订阅流**已全部迁到 bsk。2026-09-28：**稍后处理（laterhub）的两个收藏源**（抖音收藏、小黑盒收藏）迁到 bsk。2026-11-02：**B 站稍后看与视频摘要也迁到 bsk**（页内 fetch 官方接口），订阅流、laterhub、视频摘要全部只走 bsk；copy 后端与 `bilibili_main.json` 已彻底删除。

## 一句话定位

bsk 是一个通过浏览器扩展驱动**本机日常 Chrome** 的抓取工具：浏览器抓取**只走 bsk**，抓取时直接复用你日常 Chrome 里已登录的会话，不复制 profile、不导出 token。copy（复制 profile）后端已整体禁用，没有任何配置或按钮能切回它。

## 稍后处理（laterhub）四源

- 抖音收藏、小黑盒收藏、B站稍后看、微信读书书架：**全部走 bsk**，复用日常 Chrome 实时登录会话。
- 四个 bsk 源共用一个 bsk 会话；某源超时只停该 bsk 会话，**不会关闭或重开你的日常 Chrome**。
- 小黑盒收藏：bsk 无请求/响应拦截能力，改为在页内借前端 `$hbRequest` 拿签名后直接取数（不再用 `page.on("request")`）。
- 微信读书书架：走 bsk 复用日常 Chrome 登录态；页内直接调同源接口 `/web/shelf/bookIds` + `/web/book/publicinfos`（页内 fetch 自动带登录 cookie，无需导出 token）。书架内可解析的书全部入库（不按「读完」标记过滤），坏 `bookId` 单条跳过、不拖垮整批。
- B 站稍后看：与订阅动态同一姿势——先导航到 watchlater 页，再页内 fetch 官方接口 `x/v2/history/toview/web`（`credentials: include` 自动带日常 Chrome cookie）。不再读 `bilibili_main.json`、不再需要手动「同步登录态」；报 `-101 账号未登录` 时在日常 Chrome 登录 B站即可。

## 订阅流里的 B 站关注动态

- **落脚在 `https://www.bilibili.com/watchlater/list`，不在 `t.bilibili.com`**：页内 fetch `feed/all` 只需同站 Origin + 登录 cookie，落脚页只是取数环境。`t.bilibili.com` 是自更新页（计时器/轮播/推荐卡片持续变化），而 `evaluate` 是无目标动作、bsk 对它保持**整页**基线校验，sync 后页面一变就回 `permission_denied: The action's observed content changed`——2026-11-04 07:14 就是连拒 6 次导致整源失败；实测同页「连拒 3 次 → 整源失败」的出现率约 1/3，换稳定页后 0/5 被拒。
- 逐 UP 的 `feed/space` 仍落脚该 UP 的 space 页（测试沿用该断言）。space 页同样自更新，若将来重新启用逐 UP 源，需按同一办法换稳定页 + 传 `host_mid`。

## 视频摘要（video_digest）

- 手动 CLI 子流程（`python -m apps.laterhub.video_digest`），不进主抓取流水线。
- B站素材全部走 bsk：在**稳定的 watchlater 页**内 fetch `x/web-interface/view`、`x/player/v2`（字幕轨道）、`x/player/playurl`（dash/durl）。
  不用视频播放页取数——弹幕/计时器持续自更新，整页基线校验必然判 changed，evaluate 会被拒。
- 字幕 JSON（签名 URL）与媒体 CDN 流不需要 Cookie：前者直接 HTTP 拉取，后者只需浏览器 UA + Referer（实测 206）。
- 抖音分支仍是免签直链下载 + 本地 ASR，与 B站无关。


## 版本与更新

- **CLI / daemon**：`bsk update` 升级，`bsk update --check` 只检查不升级。
- **浏览器扩展**：走浏览器扩展自己的更新渠道，`bsk update` **无法更新扩展**。
- **扩展升级后 CLI 会自动跟上**：上游把 CLI 与扩展**成对发布**、协议需一致，而扩展会在 Chrome 里静默升级、CLI 不会自己动。故 `check_bsk_update.ps1` 在下列任一条件成立时直接 `bsk update -y` 把 CLI 追平（这是该脚本唯一的写动作，只在真需要时发生）：
  - 已连接浏览器的扩展版本高于 CLI；
  - `version_skew_browsers` 非空；
  - **读不到任何扩展版本**（daemon 连不上 / 没有已连接浏览器）**且 CLI 有新版**——这是 2026-10-31 事故暴露的盲区：扩展太新时连不上 daemon，`browsers` 为空，纯版本比较永远判不出「扩展领先」，检查天天空转，直到 11-02 晨间全量浏览器源失败。
  升级后脚本会 `bsk daemon stop`：`bsk update` 只换二进制、**不重启由宿主持有的 daemon**（`host_managed`，实测输出 `daemon: left_to_host`），旧 daemon 与新扩展协议仍不兼容；停掉后下一次抓取的 `BskBrowser.ensure_daemon()` 会用新二进制拉起。反方向无解——CLI 更新不了扩展，只能等扩展自己升，脚本只提示。
- **两个抓取槽开工前都强制查一次**：`auto_runner._do_run_prefetch()`（7:10，当天第一个用 bsk 的时点）与 `_do_run_morning()`（8:10）都以 `force=True` 调同一个脚本。按天的戳在「扩展昨夜升级、CLI 没跟」时会拖到第二天；7:10 尤其不能漏——8:10 只补抓「资讯」源，其余浏览器源当天再没机会。
- **三者协议需一致**：`bsk status` 报 `daemon_version`、每个已连接浏览器的 `extension_version`，并用 `version_skew_browsers` 列出错配的浏览器。当前基线：CLI/daemon `0.4.1`、扩展 `0.4.1`、协议双方 `1.8`，无错配。
- **0.4+ 的动作必须先 `cooperate sync`**：协议 1.6 起，扩展只接受带「最新 control context」的动作；`evaluate` / `wheel` / `navigate` 不带上下文一律 `permission_denied`（`cooperation_sync_required`），且**一个 context 只授权一次动作**；**唯一例外是会话里的第一个动作**（新 tab 上内容脚本未注入，sync 本身也拿不到上下文）。`connectors/_shared/bsk_browser.py` 的 `_run_action()` 已内聚该协议：先 `cooperate sync --policy strict` 取 `context_id` + `observation_id`，再带 `--context`（脚本动作另带 `--observation`）执行；页面在 sync 后变化导致的 `permission_denied`（`effect_state=none`，即什么都没执行）会自动重新 sync 重试。`navigate()` 走「先无上下文尝试，被拒后再 sync + 带上下文重试」，否则共享会话里只有第一个源能导航。导航刚完成时内容脚本尚未注入新文档，sync 会回 `Could not establish connection. Receiving end does not exist.`，故 sync 重试窗口按秒级给足（6 × 0.6s）。`--policy strict` 是扩展 1.7 也能用的策略，`target` 需双方 1.8。

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
- **脚本必须按 UTF-8 捕获 bsk 输出**：Windows PowerShell 5.1（web 服务用 `powershell -NoProfile -File` 调它）按控制台代码页解码原生命令输出，本机 936 会把 `bsk status --json` 的浏览器标签 `main浏览器` 解成乱码并吞掉收尾引号，`ConvertFrom-Json` 必失败。2026-11-02～11-04 日志里的「daemon 状态未知」全由此而来：扩展版本与 `version_skew_browsers` 全程不可见，正好瞎掉本脚本的唯一职责。脚本开头已固定 `[Console]::OutputEncoding = UTF8`；若再出现 `daemon 状态未知（bsk status 失败：传入的对象无效…）`，先查这行是否还在。
- 手动检查：
  - `bsk update --check --json` 看 CLI 是否有更新
  - `bsk status --json` 看 daemon 与扩展版本、是否错配

## 抓取出错时的排查顺序

1. 先 `bsk update --check`：CLI/扩展版本错配是常见故障源，有更新就先更新 CLI（扩展另走扩展渠道）。
2. `bsk status`：确认 daemon 在跑、扩展已连接（看 `browsers` 列表与 `version_skew_browsers`）。
   若报 `os error 5 拒绝访问`，**不是 daemon 没跑，而是调用方权限级别低于 daemon**（见上文「运行前提」）。
3. 确认 Chrome 开着且扩展在线（扩展离线时 daemon 收不到浏览器会话）。
4. 看源表「最近错误」：判断是否某源登录态失效，需在**日常 Chrome 重新登录**该站点。
5. 报 `evaluate 失败 (exit=1, code=permission_denied)` / `wheel 失败 (…permission_denied)`：daemon 说的是「The action's observed content changed; synchronize and replan」，即 sync 之后页面又变了。`evaluate` 是无目标动作，bsk 对它保持**整页**基线校验，自更新页面天然容易撞上；连接器已按 `effect_state=none` 自动重试（单动作 3 次 × 会话 2 次）。**修法是换稳定落脚页**（B 站关注流/稍后看/视频摘要都已如此），靠加重试不解决——自更新页会连续拒。导航刚完成时 `cooperate sync` 被拒属正常（内容脚本尚未注入新文档），`_sync_context` 自带 6 × 0.6s 重试窗口。
6. 以上都排除后仍失败，直接修 bsk 链路（`bsk update` / `bsk status` / 扩展）；copy 后端已禁用，不存在「换后端重试」的退路。