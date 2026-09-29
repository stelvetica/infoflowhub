# 注册 InfoFlowHub 开机自启计划任务（服务 + bsk daemon，单一任务）
#
# 触发点是"登录时"，并每 5 分钟重复一次：浏览器抓取依赖桌面会话里的 Chrome 与 bsk 扩展，
# 登录前（session 0）没有浏览器。本机账户无密码，开机后约 12 秒自动登录，故效果等同开机自启。
# 重复触发是常驻看护——bsk daemon 闲置 10 分钟自退，靠它把栈反复拉起来（详见下方 trigger 注释）。
#
# 重装系统或换机后，以当前用户身份运行本脚本即可重建：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_autostart_task.ps1
$ErrorActionPreference = 'Stop'

$taskName = 'InfoFlowHub 开机自启（服务 + bsk daemon）'
$legacyName = 'bsk daemon (BrowserSkill)'
$wrapper = Join-Path $PSScriptRoot 'start_infoflow_stack.ps1'
if (-not (Test-Path $wrapper)) { throw "找不到自启入口脚本: $wrapper" }

$ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

# 幂等：同名任务与历史遗留的单 daemon 任务都先移除
foreach ($n in @($taskName, $legacyName)) {
    if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $n -Confirm:$false
        Write-Output "已移除旧任务: $n"
    }
}

$action = New-ScheduledTaskAction -Execute $ps `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$wrapper`""

$user = "$env:USERDOMAIN\$env:USERNAME"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
# 常驻看护：bsk daemon 闲置 10 分钟会自退（daemon 日志实测 `daemon exceeded idle threshold;
# exiting, idle_secs=600`），只在登录时启动一次会让定时抓取撞上它不在——2026-09-29 08:12 的
# 抖音/小黑盒就是这样被整源跳过的。故加重复触发：每 5 分钟一次；任务实例还在跑（daemon 活着）
# 时由 MultipleInstances=IgnoreNew 跳过，daemon 一退，下一个 tick 就把栈重新拉起。
# 注：抓取侧另有 BskBrowser.ensure_daemon() 兜底（开工前探活并按需拉起），这里是第二道保险。
$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$trigger.Repetition = $watchdog.Repetition

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

# 必须用当前用户身份（SYSTEM 会话里没有用户的 Chrome 与扩展），且必须是 Highest：
# 本任务启动的 daemon 会拒绝低完整性客户端的命名管道连接（实测 os error 5 拒绝访问），
# 而同级的「bsk 更新检查」任务（/RL HIGHEST）必须能连上它。用 Limited 会让 daemon 与
# 客户端权限级别不一致，检查任务误报"daemon 未运行"。
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description '登录时拉起并在其后每 5 分钟检查一次：自动拉起 InfoFlowHub Web 服务（先探活，已在跑则跳过，避免误杀健康实例与 cloudflared 隧道）与 bsk daemon（前台持有，崩溃自动重启）。重复触发是常驻看护——bsk daemon 闲置 10 分钟自退，靠它把栈反复拉起来；抓取侧另有 BskBrowser.ensure_daemon() 兜底。本机账户无密码，开机后约 12 秒自动登录，故等同开机自启；浏览器抓取依赖该桌面会话，不能在登录前运行。' | Out-Null

Write-Output "任务已注册：$taskName"
$t = Get-ScheduledTask -TaskName $taskName
$i = Get-ScheduledTaskInfo -TaskName $taskName
Write-Output ("状态      : {0}" -f $t.State)
Write-Output ("运行身份  : {0} / 提权级别 {1}" -f $t.Principal.UserId, $t.Principal.RunLevel)
Write-Output ("动作      : {0}" -f $t.Actions[0].Arguments)
Write-Output ("触发      : {0}" -f ($t.Triggers | ForEach-Object { $_.CimClass.CimClassName }))
Write-Output ("上次结果  : {0}" -f $i.LastTaskResult)