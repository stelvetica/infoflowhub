# 注册「每 14 天检查 bsk 更新」计划任务（本脚本只写不执行，注册由人工确认后手动跑）。
#
# 以当前用户身份、**提权（/RL HIGHEST）**、隐藏窗口运行 scripts\check_bsk_update.ps1。
# 「每 14 天」用 schtasks /SC DAILY /MO 14 表达：语义即"每 14 天一次"，起点为 /ST 指定时刻。
#
# 为什么必须 HIGHEST 而不是 LIMITED：bsk daemon 由「InfoFlowHub 开机自启」任务以 Highest
# 启动，它创建的命名管道会**拒绝低完整性（LIMITED）客户端的连接**（实测报 os error 5 拒绝访问：
# 管道存在、52800 端口可达，纯粹是权限）。用 LIMITED 跑本任务时 `bsk status` 必然失败，
# 检查结果会误报"daemon 未运行"。故本任务与 daemon 保持同一权限级别。
#
# 重装系统或换机后，以当前用户身份运行本脚本即可重建：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_bsk_update_task.ps1
$ErrorActionPreference = 'Stop'

$taskName = 'bsk 更新检查（每14天）'
$script = Join-Path $PSScriptRoot 'check_bsk_update.ps1'
if (-not (Test-Path $script)) { throw "找不到检查脚本: $script" }

$ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$action = "`"$ps`" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""

# /SC DAILY /MO 14 = 每 14 天一次；/ST = 起点时刻；/RL HIGHEST = 提权（必须，见上）。
# 默认即以当前用户交互身份运行（仅登录时），不需要额外 /RU。
schtasks /Create /TN $taskName /TR $action /SC DAILY /MO 14 /ST 10:00 /RL HIGHEST /F

Write-Output "任务已注册：$taskName（每 14 天 10:00 运行）"
schtasks /Query /TN $taskName /V /FO LIST