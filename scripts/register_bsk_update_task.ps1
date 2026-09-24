# 注册「每 14 天检查 bsk 更新」计划任务（本脚本只写不执行，注册由人工确认后手动跑）。
#
# 以当前用户身份、不提权（/RL LIMITED）、隐藏窗口运行 scripts\check_bsk_update.ps1。
# 「每 14 天」用 schtasks /SC DAILY /MO 14 表达：语义即"每 14 天一次"，起点为 /ST 指定时刻。
#
# 重装系统或换机后，以当前用户身份运行本脚本即可重建：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_bsk_update_task.ps1
$ErrorActionPreference = 'Stop'

$taskName = 'bsk 更新检查（每14天）'
$script = Join-Path $PSScriptRoot 'check_bsk_update.ps1'
if (-not (Test-Path $script)) { throw "找不到检查脚本: $script" }

$ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$action = "`"$ps`" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""

# /SC DAILY /MO 14 = 每 14 天一次；/ST = 起点时刻；/RL LIMITED = 不提权。
# 默认即以当前用户交互身份运行（仅登录时），不需要额外 /RU。
schtasks /Create /TN $taskName /TR $action /SC DAILY /MO 14 /ST 10:00 /RL LIMITED /F

Write-Output "任务已注册：$taskName（每 14 天 10:00 运行）"
schtasks /Query /TN $taskName /V /FO LIST