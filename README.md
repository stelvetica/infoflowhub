# infoflowhub

这是整个信息跟踪体系的总仓库。

当前按功能分成三条并列主线：

- `laterhub/`：个人筛选类内容
- `subscriptions/`：个人订阅类内容
- `newshub/`：新闻获取类内容

其中目前只有 `laterhub/` 已完成第一版，可用于把个人主动筛选后准备稍后处理的内容统一收集、打标签、入库并推送到飞书。

## 当前结构

```text
infoflowhub/
├─ README.md
├─ laterhub/
├─ subscriptions/
└─ newshub/
```

## 各目录说明

### `laterhub/`

当前已落地的子系统，负责“个人筛选类”内容收集。

已接入：

- B 站稍后看
- 抖音收藏

主流程：

- 抓取内容
- 写入本地 SQLite
- LLM 自动打标签
- 推送到飞书多维表格
- 从飞书回写已完成状态

运行入口见 [laterhub/README.md](/C:/Users/TB14Plus/infoflowhub/laterhub/README.md)。

### `subscriptions/`

后续用于“个人订阅类”内容。

计划承接：

- RSS
- 博客订阅
- 邮件简报
- 频道订阅

### `newshub/`

后续用于“新闻获取类”内容。

计划承接：

- 新闻抓取
- 事件流整理
- 主题新闻编排

## 仓库定位

这个仓库当前还不是完整的信息跟踪系统，只是先把“个人筛选类”的框架和第一批能力落地。
