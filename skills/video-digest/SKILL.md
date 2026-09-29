---
name: video-digest
description: 把稍后阅读里的视频做成简明扼要的文字介绍（视频简报）。触发条件：用户要求"视频简报/总结稍后阅读里的视频/生成视频介绍/总结B站视频/把稍后看的视频做成笔记"时使用。自动按三级链路取材：B站 CC 字幕直取 → 下载视频 + 本地语音转写 → GLM 视频理解兜底。独立于稍后处理面板，不改动面板 UI。
---

# 视频简报（video-digest）

把 laterhub（稍后阅读）里的视频条目做成一篇简明扼要的介绍，落盘 md 供阅读。完全独立于稍后处理面板：面板零改动，本 skill 是唯一入口。

## 路径约定（固定值，无需向用户确认）

| 用途 | 路径 |
|---|---|
| 项目根（命令工作目录） | `C:\Users\ETT\Playground\infoflowhub` |
| Python 解释器（必须用项目 venv） | `C:\Users\ETT\Playground\infoflowhub\.venv\Scripts\python.exe` |
| laterhub 数据库 | `C:\Users\ETT\Playground\infoflowhub\data\laterhub.sqlite3` |
| 产物根 | `C:\Users\ETT\Playground\infoflowhub\data\reports\` |
| 简报产物 | `<产物根>\video_digest_<平台>_<id>.md` |

## 能力链路（编排内自动降级，AI 无需干预）

1. **B站 CC 字幕直取**（首选）：官方接口拿 AI 字幕全文，秒级、免下载。
2. **下载 + 本地语音转写**：无 CC 时下载视频流、ffmpeg 合流，`videocaptioner --asr bijian` 转写（免费，仅中英文）。
3. **GLM 视频理解兜底**：前两步失败时，把完整 mp4 直链直接交给多模态模型产出总结。

每份简报标注「字幕来源」与「备注」（材料不完整——如试看片段、转写误差——模型会在备注里如实说明，不编造）。

## 流程

### 1. 列候选（--dry-run，不消耗资源）

```powershell
cd C:\Users\ETT\Playground\infoflowhub
.venv\Scripts\python.exe -m apps.laterhub.video_digest --dry-run --limit 10
```

输出待处理视频条目（id / 标题）。也可加 `--source bilibili_watchlater` 过滤来源。

### 2. 生成简报

```powershell
# 批量限量（默认只处理 pending，不会重复生成）
.venv\Scripts\python.exe -m apps.laterhub.video_digest --limit 3

# 指定条目（面板/数据库里看到的 id）
.venv\Scripts\python.exe -m apps.laterhub.video_digest --id 1263

# 连同失败条目重试
.venv\Scripts\python.exe -m apps.laterhub.video_digest --limit 5 --retry-failed
```

用户说"总结最新 N 个视频"→ 用 `--limit N`；"总结这条"并给出标题/链接 → 先 `--dry-run` 找到 id 再 `--id <id>`。

### 3. 汇报（必做，不得只报"完成"）

逐条读产物 md，在对话里给出速览表：

| 条目 | 结果 | 一句话总结 |
|---|---|---|
| 国家队偷偷加仓ETF | ✅ 简报 | 用三因子概率监控国家队增持 ETF 的信号与局限 |

- 失败条目如实说明原因（如"试看片段只有开头 35 秒"）。
- 抖音条目输出 `[SKIP] 抖音暂未支持`：如实转告"抖音视频需免签直链，暂未接入"，不要硬凑。
- 全部成功后可用 `python -c "import os; os.startfile(r'<产物绝对路径>')"` 打开第一份产物。

## 硬约束

- **只用上面 venv 的 python**：系统 python 缺依赖（dotenv/requests 版本不对会静默降级）。
- 幂等：已成功的条目自动跳过，重复运行安全；只有 `--retry-failed` 才重试失败条目。
- 简报格式由代码固定（标题/来源/链接/作者/时长/字幕来源/一句话总结/核心要点/备注），**不要手改产物结构**；对内容不满意就改跑一次或反馈调整 LLM 约束。
- 产物进面板「生成产物」页（白名单 `video_digest_*.md`），**稍后处理面板不出现任何简报按钮**（用户已明确要求面板保持原样）。
- 单次批量建议 ≤5 条：每条涉及下载/转写/模型调用，耗时 10 秒～3 分钟不等。
