# laterhub

一个把“个人主动筛选出来、准备以后再看”的内容集中汇总的项目。

它当前不负责完整新闻系统，也不是通用 PKM 平台。
它更准确的定位是：

- 收集个人筛选类内容
- 统一入库
- 自动打标签
- 推送到统一待处理列表

当前已接入：

- B 站稍后看
- 抖音收藏

当前主流程：

- 抓取内容
- 写入 SQLite
- LLM 自动打标签
- 推送到飞书多维表格
- 从飞书回写已看完状态

## 适用范围

这个项目主要服务于“信息跟踪”体系里的“个人筛选类”内容，不是下面这些方向：

- 不是 RSS 订阅器
- 不是新闻聚合器
- 不是市场新闻生成器
- 不是阅读器本身

它更像“个人稍后看 / 收藏内容的统一收件箱”。

## 目录结构

```text
laterhub/
├─ .env
├─ .env.example
├─ README.md
├─ run_auto.py
├─ connectors/
│  └─ sites/
│     ├─ bilibili/
│     │  └─ fetch.py
│     └─ douyin/
│        └─ fetch.py
├─ services/
│  ├─ config.py
│  ├─ feishu.py
│  └─ tagger.py
├─ storage/
│  └─ db.py
├─ workflow/
│  ├─ pipeline.py
│  └─ sync_finished.py
├─ scripts/
│  └─ manual/
│     └─ open_douyin_login.py
└─ runtime/
   ├─ data/
   ├─ logs/
   ├─ debug/
   └─ browser_profiles/
```

## 运行方式

安装依赖：

```bash
pip install requests python-dotenv playwright
playwright install chromium
```

复制 `.env.example` 为 `.env` 后填写配置。

主流程：

```bash
python run_auto.py
```

指定来源：

```bash
python run_auto.py --fetch-bilibili
python run_auto.py --fetch-douyin
python run_auto.py --fetch-bilibili --fetch-douyin
```

重试失败记录：

```bash
python run_auto.py --retry-failed
```

同步飞书已看完状态：

```bash
python -m workflow.sync_finished
```

抖音登录辅助：

```bash
python scripts/manual/open_douyin_login.py
```

## 环境变量

飞书：

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_BITABLE_APP_TOKEN`
- `FEISHU_BITABLE_TABLE_ID`

B 站：

- `BILIBILI_COOKIE`
- `BILIBILI_SESSDATA`
- `BILIBILI_BILI_JCT`
- `BILIBILI_DEDEUSERID`

标签模型：

- `PRIMARY_LLM_BASE_URL`
- `PRIMARY_LLM_API_KEY`
- `PRIMARY_LLM_MODEL`

可选备用模型：

- `BACKUP_LLM_BASE_URL`
- `BACKUP_LLM_API_KEY`
- `BACKUP_LLM_MODEL`

## 扩展方式

后续如果继续接“小红书收藏、Raindrop、微信读书、知乎收藏、小黑盒收藏”等，也应该继续按这个模式扩展：

1. 在 `connectors/sites/新站点/` 下新增抓取器。
2. 输出统一字段：`url`、`title`、`source`、`tags`。
3. 在 `workflow/pipeline.py` 中接入。

## 当前位置

这个模块现在已经作为 `infoflowhub/` 下的一个子项目存在：

- `laterhub/`：个人筛选类
- `subscriptions/`：个人订阅类
- `newshub/`：新闻获取类

## 不提交到 GitHub 的内容

- `.env`
- 数据库
- 日志
- 调试文件
- 浏览器登录态
- `__pycache__`
