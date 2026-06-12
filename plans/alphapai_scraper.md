# Alpha派蓝宝书 — 每日自动抓取并入订阅流水线

**版本:** v1.0
**日期:** 2026-06-12
**状态:** 已实施

---

## 1. 背景

蓝宝书是 Alpha派 (`alphapai-web.rabyte.cn`) 每日发布的投研报告合集，每天约 4 版（全球版、晨会版、晚间版、午间版）。

**约束条件:**
- SPA 页面（Element UI），必须浏览器渲染
- 需要登录态，且同一时间只能一台浏览器登录
- 列表页只展示标题+摘要，正文必须逐条点击进入详情页
- 页面结构规整，按时间倒序排列

---

## 2. 架构

```
Windows Task Scheduler (每天 08:30)
  └→ scripts/run_alphapai_pipeline.bat
       ├─ taskkill /im chrome.exe /t    ← 优雅关闭日常Chrome
       ├─ timeout /t 3                   ← 等锁释放
       ├─ python -m apps.subscriptions.rss_pipeline fetch --source alphapai
       │    └→ web_fetch.py → _fetch_web_source_once()
       │         ├─ kill_chrome_gracefully()
       │         ├─ launch_alphapai_context()  ← Playwright 复用 Default Profile
       │         ├─ page.goto(蓝宝书URL)
       │         ├─ connectors/alphapai/feed.py::fetch_alphapai_with_page()
       │         │    ├─ 滚动加载直到连续2条日期≤截止日期
       │         │    ├─ 正则提取列表
       │         │    ├─ 逐条点击进入详情
       │         │    ├─ 免责声明锚点确认加载完成
       │         │    ├─ HTML → Markdown 保留格式
       │         │    └─ 返回 FeedFetchResult
       │         └─ browser.close()
       └→ 存入 subscriptions.sqlite3
```

---

## 3. 文件清单

### 3.1 新增文件

| 文件 | 职责 |
|------|------|
| `scripts/run_alphapai_pipeline.bat` | 定时任务入口，先关 Chrome 再抓取 |
| `connectors/alphapai/__init__.py` | 导出 `fetch_alphapai_with_page` |
| `connectors/alphapai/feed.py` | 核心抓取逻辑（正则提取 + 点击详情 + HTML→MD） |

### 3.2 修改文件

| 文件 | 改动 |
|------|------|
| `connectors/_shared/common.py` | `ALPHAPAI_PROFILE_DIR` + `is_chrome_running()` + `kill_chrome_gracefully()` + `launch_alphapai_context()` + `resolve_web_target` alphapai case |
| `connectors/_shared/web_fetch.py` | alphapai 路由分支（自动关闭 Chrome） |
| `connectors/auth/registry.py` | `AUTH_REGISTRY["alphapai_main"]` |
| `connectors/auth/providers/browser_profiles.py` | `ALPHAPAI_PROFILE_DIR` + `validate_alphapai_auth()` + `get_context_path` mapping |
| `config/subscription_sources.json` | alphapai 订阅源 |

---

## 4. 关键设计决策

### 4.1 复用系统 Chrome Default Profile

- X 平台用 `Profile 2`，蓝宝书用 `Default`（你日常登录的 Profile）
- 抓取前自动 `taskkill /im chrome.exe /t`（不加 `/f`，保存标签页）
- 抓取后不自动启动 Chrome（由 Task Scheduler 控制窗口期）

### 4.2 去重/停止策略

1. 查 `subscriptions.sqlite3` 中 `source_id='alphapai'` 的最大 `published_at`
2. 滚动加载时，连续 2 条日期 ≤ 截止日期 → 停止
3. `save_entries` 的现有 `title` 去重兜底（title 格式: `"全球 6月12日 全球版"`）

### 4.3 详情页加载确认

用「免责声明」作为锚点信号——每篇报告底部固定有「免责声明/不构成任何投资建议」，出现即确认加载完成。

### 4.4 HTML → Markdown 格式保留

标题层级、加粗、段落、列表等结构通过正则转为 Markdown，存到 `FeedEntry.summary`。

---

## 5. 定时任务配置

```
Windows Task Scheduler:
  触发器: 每天 08:30
  操作: C:\Users\TB14Plus\Playground\infoflowhub\scripts\run_alphapai_pipeline.bat
  条件: 仅当交流电源时运行
```

---

## 6. 与现有 TM 脚本的关系

| | Tampermonkey 脚本 | 自动化流水线 |
|---|---|---|
| 文件 | `scripts/alphapai_batch_download.user.js` | `connectors/alphapai/feed.py` |
| 用途 | 手动应急 + 调试 | 无人值守定时抓取 |
| 执行频率 | 按需 | 每天 08:30 |
| 存储 | 浏览器下载 .md 文件 | subscriptions.sqlite3 |

---

## 7. 首次使用

1. 确认 Chrome 已登录 `alphapai-web.rabyte.cn`，蓝宝书页面可正常访问
2. 关闭 Chrome
3. 运行测试:
   ```batch
   cd C:\Users\TB14Plus\Playground\infoflowhub
   C:\Users\TB14Plus\.workbuddy\binaries\python\versions\3.13.12\python.exe -m apps.subscriptions.rss_pipeline fetch --source alphapai
   ```
4. 检查输出 `data/subscriptions.sqlite3` 中 `rss_entries` 表
5. 确认无误后配置 Windows Task Scheduler
