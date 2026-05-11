# debug

这个目录用于存放 PKM Auto-Hub 的一次性排障脚本与调试产物。

说明：
- `tmp_*.py` / `tmp_*.json` 都属于阶段性验证材料，不是正式主流程的一部分。
- 当前正式入口只有：
  - `main.py`
  - `fetch_bilibili.py`
  - `db_manager.py`
  - `feishu_api.py`
- 当某条链路稳定后，应优先把调试脚本删除，或保留最少量可复用的诊断脚本。

当前保留原则：
- 与现行主方案无关的旧排障脚本，允许删除。
- 与 Playwright 登录态诊断直接相关、后续可能复用的脚本，可以暂时保留在 `debug/`。
