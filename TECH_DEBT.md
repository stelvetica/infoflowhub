# 技术债登记

有意简化、且已知天花板的地方。每条给出位置、天花板与升级路径，便于集中偿还。

| 位置 | 天花板 | 升级路径 |
| --- | --- | --- |
| `web/static/app.js` — 稍后处理「完成」就地移除行（`applyLaterhubToggle`） | 续载按绝对偏移取页（`laterhub_page=N`）；在「未处理/已打开」筛选下完成一条会把列表左移一位，此后继续下滚可能**跳过一条**（刷新或改筛选即恢复，不会丢数据） | 改成游标式续载：按最后一条的排序键（`sort_time + id`）取下一页，前后端各加一个 `after=` 参数 |
| `connectors/alphapai/social.py` — 社媒卡片 link 统一回落默认页 | 卡片 DOM 无独立链接，同一源全部条目共用一个 link：点条目只打开聚合页而非该篇文章；该源因此被排除在「按 link 去重 / link 唯一索引」之外（`rss_db.SHARED_PAGE_SOURCE_IDS`），跨源 link 去重对它失效 | 找到卡片级稳定链接（点击后 URL 或接口返回的 article id）后写入 link，并把该源移出 `SHARED_PAGE_SOURCE_IDS` |
| `connectors/_shared/bsk_browser.py` — start 超时后按 `created_at_ms >= 本次 start 时刻` 清孤儿 session | 判据只有「新建时间」：若同一时刻另有进程在起 bsk session，会被误清（单用户本地工具下概率极低） | bsk 支持按 `--name` 在 `session list --json` 回读时，改用本次唯一的 name 精确匹配 |
| `web/services/md_render.py`（极简 Markdown 渲染器） | 只支持产物实际用到的语法子集（ATX 标题、有序/无序列表、单行管道表、加粗、行内码、链接、空行分段）；不支持嵌套列表、代码围栏与高亮、引用块、图片、脚注、表格合并单元格。产物一旦用到这些语法会退化为纯文本 | 换 `mistune`（`render = mistune.html`，或函数内改调它），调用方 `web/routes/main.py` 无需改动 |
| `web/services/views.py` `read_log_tail()`（稍后处理运行日志尾部） | 以固定起始行标记切片取最近一次运行，省去结构化日志头；日志格式变更（标记文案改动）时会退化为取全部行 | 改为结构化日志（每次运行写独立段/JSON），或按标记正则匹配多套文案 |
| `collect_announcements.py`（portfolio-announcements skill，非本仓）重写清单 | AI 层捞回/删除的行不进 `seen` 留痕，被捞回的行（脚本正则误剔）在同日重跑时仍判为"新增"→重写清单并把已补的介绍重置为"待补" | 重写时解析旧清单、按 art_code 保留已有介绍格；或 AI 层定稿后把最终 art_code 回写 `seen` |
| `tests/` 全量测试的 pytest 依赖未声明 | `pytest` 不在 `pyproject.toml`/`uv.lock` 中，只是手动 `uv pip install` 进了 `.venv`；重建 venv 或 `uv sync` 后 `uv run python -m pytest tests/ -q` 直接报 `No module named pytest`（新 worktree 必然撞上，每次都要手动补装） | 在 `pyproject.toml` 加 dev 依赖组（如 `[dependency-groups] dev = ["pytest"]`）并同步 `uv.lock` |
| `web/services/auto_runner.py` `_flow_products()`（本次流程产物判定） | 按「白名单文件 mtime >= flow.started_at」判，且只扫**本服务所在仓库**的 `data/reports`；而 skill 的产物根在 SKILL.md 里写死为主 checkout 路径——服务不在主 checkout 跑（如 worktree）时会「skill 全成功却 0 产物」（现已降级为 partial 并写 error 提示，但根因未消） | 让产物根由调度方经参数/环境变量传入 skill，或在 skill 收尾时校验落点并回报 |
| `web/services/skill_runner.py`（skill 子进程调度） | 只判退出码：不解析 CLI `--json` 的 usage（无成本统计）、失败不重试（靠次日调度或人工重跑）；`_kill_process_tree` 在非 Windows 只杀父进程，node 派生的子进程会留孤儿（本仓部署在 Windows，未暴露） | 接入 usage 做成本统计；给 transient 失败加一次退避重试；跨平台改 `start_new_session=True` + `os.killpg` |
| `web/templates/partials/news_flow_status.html`（流程状态条） | 终态页面不会自己发现「新流程开始」（那需常驻轮询，与「空闲零请求」冲突）；页面在流程开始前打开并停在终态时，要手动刷新才看得到运行态 | 把「流程开始」也做成事件源（SSE 或独立轮询端点），终态时才按需订阅 |
| `web/services/auto_runner.py` `_open_panel()` | 用 `webbrowser.open` 唤起面板，无 GUI / 无默认浏览器时静默失败（不影响流程结果，但用户拿不到跳转） | 改成显式指定浏览器可执行文件的 subprocess 调用，失败时写入 flow.error 让状态条可见 |

| `connectors/_shared/bsk_browser.py` 的 `get_html()` 走 `bsk get-html` | 输出在 **524289 字节（512KB）处被截断**：页面尾部内联的大 JSON（如 YouTube 的 `ytInitialData`）会被切掉，依赖整页 HTML 的提取器会失效（YouTube 已实测回退到 DOM 提取器） | 改用 `evaluate` 只取需要的那段（如正则定位后切片），或等 bsk 支持分片读取后拼接 |
| YouTube 源 `laolihai_youtube` 的 `published_at` | 两条路径都从相对时间文案推算（`Nd ago` 为天级粒度），时分等于抓取时刻，实测与真实发布时间差约 10.5h（日期正确） | 改走原生 RSS `https://www.youtube.com/feeds/videos.xml?channel_id=...`（实测可用、15 条、含精确 ISO 时间戳；现因该源 `provider=web` 而根本没被读取） |
