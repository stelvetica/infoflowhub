# infoflowhub

这是整个信息流追踪体系的总仓库。

当前按功能分成三条主线：

- `laterhub/`：个人筛选后、准备稍后处理的内容
- `subscriptions/`：个人订阅类内容
- `newshub/`：新闻获取与整理类内容

当前前端控制台已迁移为 `Python Web + Jinja2 + HTMX`，Python 同时负责抓取、入库、状态维护与页面服务。

## 环境要求（重要）

本仓库通过**复制常用 Chrome 的登录态**来抓取需要登录的站点（蓝宝书、抖音收藏、小黑盒等）。复制原理是：抓取前关闭 Chrome → 复制其 `Default` profile 到隔离副本 → 用副本抓取 → 抓完重开你的 Chrome。

**Chrome ≥ 127 默认启用 App-Bound Encryption（ABE）**，会把登录 cookie 加密后绑定到本机 Windows 用户 + Chrome 安装。这会导致复制 profile 后登录 cookie 无法解密、被 Chrome 清空，抓取失效（表现为"能抓公开内容、但抓不到需要登录的内容"）。

**必须通过注册表策略关闭 ABE**（仅 Windows）：

```cmd
reg add "HKLM\SOFTWARE\Policies\Google\Chrome" /v ApplicationBoundEncryptionEnabled /t REG_DWORD /d 0 /f
```

- 关闭后**重启 Chrome** 使策略生效。
- 关闭后**需重新登录各站点**（旧 ABE 加密 cookie 无法被工具读取，重登后生成可复制的 DPAPI(v10) 格式）。
- 验证：`chrome://policy` 中 `ApplicationBoundEncryptionEnabled` 应为 `false`。
- 恢复安全：`reg delete "HKLM\SOFTWARE\Policies\Google\Chrome" /v ApplicationBoundEncryptionEnabled /f`。
- **防写回保护**（2026-09-01 已启用）：该注册表键已加 Deny-WriteKey ACL，普通/管理员进程均无法直接改写策略值（Deny 优先于 Allow）。若确需调整，须先在注册表编辑器（需管理员）中移除 `Authenticated Users` 的 Deny 规则再操作。
- Mac/Linux 无 ABE 机制，无需此操作。

> ⚠️ 关闭 ABE 会降低本机 Chrome 本地 cookie/密码的安全性，仅在本机运行抓取时建议如此。

登录态来源：**常用 Chrome 的 `Default` profile**。各站点需先在常用 Chrome 里登录（见 `scripts/login_chrome.py` 一键拉起所有站点登录页）。

### 首次配置流程（新环境 / 换机后）

1. **关闭 ABE**（见上，Windows 才需要）：
   ```cmd
   reg add "HKLM\SOFTWARE\Policies\Google\Chrome" /v ApplicationBoundEncryptionEnabled /t REG_DWORD /d 0 /f
   ```
2. **重启 Chrome** 使策略生效（`chrome://policy` 确认值为 `false`）。
3. **拉起所有站点登录页并逐一登录**：
   ```cmd
   python scripts\login_chrome.py
   ```
   登录态会存到常用 Chrome 的 `Default` profile，成为复制登录态的来源。
4. **启动控制台**并触发一次「立即抓取」验证登录态复制生效。

> 换机后若某需登录站点抓不到（能抓公开内容、抓不到登录内容），多半是 ABE 未关或该站未重登。

## 当前结构

```text
infoflowhub/
├─ web/                  # Python Web 服务、模板与静态资源
├─ apps/                 # Python 业务模块
├─ connectors/           # 各平台抓取器
├─ config/               # 订阅源与运行配置
├─ data/                 # SQLite 数据
├─ runtime/              # 运行状态、健康信息、日志与本地产物
└─ scripts/              # 启动、重启、登录辅助脚本
```

**`data/标的观察/`** 是指向外部业务目录（坚果云同步）的**符号链接**，为 `portfolio-*` 三个 skill 与 `scripts/publish_reports.py` 共用的落盘口：读持仓 Excel、回写投研产物。该目录搬迁时只需重建这一个链接（`rmdir` 后 `mklink /D`），脚本零改动；链接本身已 gitignore、不入库。

## 子系统说明

### `laterhub/`

负责个人筛选类内容的沉淀与后处理。

已接入：

- B 站稍后看
- 抖音收藏
- 人工补录

主流程：

- 抓取内容
- 写入本地 SQLite
- 自动打标签
- 推送到飞书多维表格
- 从飞书回写完成状态

### `subscriptions/`

负责 RSS、网页源与订阅内容聚合。

当前能力：

- 管理订阅源
- 抓取 RSS / 网页源
- 写入本地 SQLite
- 在 Web 控制台中检索、排序、筛选和维护源状态

### `newshub/`

预留给后续新闻聚合与事件流整理，当前不在主流程中。

## 启动方式

项目对 Python 解释器**环境无关**：换台机器无需手动配置，启动脚本会用该机器自带的 Python 自动就绪环境。

### 前置条件

- 目标机器装有 Python 3.12 / 3.13（`python` 在 PATH，或 `py -3.12` 可用）；首次启动需联网装依赖一次
- （可选）装了 [uv](https://docs.astral.sh/uv/) 则可改用 `uv sync --extra browser` 获得锁版本环境

### 启动控制台

- 推荐：`powershell -ExecutionPolicy Bypass -File scripts\start_infoflow_web.ps1`
- 或双击：`scripts\一键重新拉起InfoFlowHub服务.bat`

启动时 `scripts\ensure_python_env.ps1` 按序解析 Python：`INFOFLOW_PYTHON` 环境变量 → 项目内可用 `.venv` → 机器 `python` → `py` launcher；`.venv` 失效则用本机 Python 重建；依赖缺失则自动 `pip install -r requirements.txt`。

默认地址：[http://127.0.0.1:18421](http://127.0.0.1:18421)

### 开机自启：由机器配置的统一入口拉起，本仓不注册计划任务

本项目的开机自启**不在本仓注册计划任务**，而是交给机器配置仓（坚果云 `computer_config`）的单一入口：

计划任务「**开机启动-统一入口**」（登录时触发 · ETT 身份 · Highest 提权）→ 跑该仓的 `manage_startup_items.ps1 -Run` → 拉起清单里 `Launch=$true` 的条目，其中一条就是本仓的 `scripts\start_infoflow_stack.ps1`（Web 服务 + bsk daemon，脚本内部先探活，重复跑安全）。

因此本仓的两个注册脚本都**不要再注册到系统**，否则会和统一入口双开：

- `scripts\register_autostart_task.ps1`（注册「InfoFlowHub 开机自启」）——已被统一入口取代。老机上若还留着那条任务，去机器配置仓跑 `manage_startup_items.ps1 -Remove "InfoFlowHub 开机自启"` 删掉。
- `scripts\register_bsk_update_task.ps1`（注册「bsk 更新检查」）——该检查已下沉进 `web/services/auto_runner.py`（见 `docs/bsk-browser.md`），文件仅留作手动排查用。

**分层规则**（机器配置仓定，改启动项前先读它）：涉及「**开机启动**」的放进机器配置清单；涉及「**持续监控 / 定时 / 看护**」的放进各自项目、由项目启动后自己调用，别放系统层——bsk CLI 更新检查就落在 `auto_runner` 里；daemon 无常驻保活，抓取侧开工前的 `ensure_daemon()` 即「用前保活」（foreground 子进程拉起，见 `docs/bsk-browser.md`）。

### 可选：浏览器抓取器

若使用抖音 / X / YouTube / 小黑盒等浏览器抓取器，需额外安装 playwright 并下载浏览器：

```powershell
.venv\Scripts\python.exe -m pip install "playwright>=1.40,<2"
.venv\Scripts\python.exe -m playwright install chromium
```

## 版本库约束

- `config/` 中的订阅源与运行配置继续纳入版本控制
- `runtime/health/*.example.json` 保留为结构样例
- 浏览器 profile、健康状态实时文件、SQLite 数据库、日志文件均不入库

## 现状

这套系统已经具备“抓取 + 入库 + Web 管理”的基本闭环。

当前主运行链路为：

- Python 业务模块
- FastAPI 页面服务
- Jinja2 服务端渲染
- HTMX 局部交互

旧的 Next.js / Node bridge 已彻底移除，当前只保留 Python Web 主链路。
