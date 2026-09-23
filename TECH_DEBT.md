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
