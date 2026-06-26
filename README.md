# infoflowhub

这是整个信息流追踪体系的总仓库。

当前按功能分成三条主线：

- `laterhub/`：个人筛选后、准备稍后处理的内容
- `subscriptions/`：个人订阅类内容
- `newshub/`：新闻获取与整理类内容

当前前端控制台已迁移为 `Python Web + Jinja2 + HTMX`，Python 同时负责抓取、入库、状态维护与页面服务。

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
