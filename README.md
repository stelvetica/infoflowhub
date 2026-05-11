# infoflowhub

这是整个信息跟踪体系的总仓库。

当前按功能分成三条主线：

- `laterhub/`：个人筛选后、准备稍后处理的内容
- `subscriptions/`：个人订阅类内容
- `newshub/`：新闻获取与整理类内容

目前前端控制台已迁移到 Next.js，Python 仅保留抓取、入库和桥接职责。

## 当前结构

```text
infoflowhub/
├─ app/                  # Next.js App Router 页面
├─ lib/                  # 前端读取与 Python 桥接
├─ scripts/              # 启动、桥接、登录辅助脚本
├─ apps/                 # Python 业务模块
├─ connectors/           # 各平台抓取器
├─ config/               # 订阅源与运行配置
├─ data/                 # SQLite 数据
└─ runtime/              # 运行状态、健康信息、调试与本地运行产物
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
- 在 Next.js 控制台中检索、排序、维护源状态

### `newshub/`

预留给后续新闻聚合与事件流整理，当前不在主流程中。

## 启动方式

- 前端控制台：`npm run dev`
- 默认地址：[http://127.0.0.1:18421](http://127.0.0.1:18421)
- 抓取与数据操作仍通过 Python 桥接脚本调用现有能力

## 版本库约束

- `config/` 中的订阅源与运行配置继续纳入版本控制
- `runtime/health/*.example.json` 保留为结构样例
- 浏览器 profile、健康状态实时文件、SQLite 数据库、Next 构建产物均不再入库

## 现状

这套系统已经具备“抓取 + 入库 + 前端管理”的基本闭环。页面服务已完全切换到 Next.js，Python 只保留抓取与桥接职责。
