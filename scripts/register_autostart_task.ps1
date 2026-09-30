# 注册 InfoFlowHub 开机自启计划任务（服务 + bsk daemon，单一任务）
#
# 只有一个触发点：登录时。浏览器抓取依赖桌面会话里的 Chrome 与 bsk 扩展，登录前（session 0）
# 没有浏览器；本机账户无密码，开机后约 12 秒自动登录，故效果等同开机自启。
#
# 2026-09-30 起本任务只干"开机把栈拉起来"一件事：原先挂在这里的「每 5 分钟看护触发器」
# （兜 bsk daemon 闲置 10 分钟自退）已下沉进项目自己——见 web/services/auto_runner.py 的
# _keep_bsk_daemon_alive()；独立的「bsk 更新检查」计划任务同样下沉为 _maybe_check_bsk_update()。
# 系统层只留开机自启，符合"定时/看护由项目自己调用"的分层约定。
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

# 只保留登录触发器。历史背景留着备查（别把看护又搬回系统层）：
# bsk daemon 闲置 10 分钟会自退（daemon 日志实测 `daemon exceeded idle threshold; exiting,
# idle_secs=600`），只在登录时拉起一次，定时抓取就会撞上它不在——2026-09-29 08:12 的抖音/
# 小黑盒就是这样被整源跳过的。当时加的「每 5 分钟重复触发器」兜住了它，但那是把项目的看护
# 责任放到系统层。现在这份责任在项目内部：
#   · 常驻保活：auto_runner._keep_bsk_daemon_alive()（每个 tick 探活并按需拉起）
#   · 抓取兜底：BskBrowser.ensure_daemon()（开工前探活并按需拉起，最硬的一道）
#   · 更新检查：auto_runner._maybe_check_bsk_update()（每 14 天，原先是一条独立的计划任务）
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $user

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

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $logonTrigger `
    -Settings $settings -Principal $principal `
    -Description '登录时拉起 InfoFlowHub Web 服务（先探活，已在跑则跳过，避免误杀健康实例与 cloudflared 隧道）与 bsk daemon（先探活，已在跑则跳过；未跑才前台持有，崩溃自动重启）。bsk 的常驻保活与更新检查已下沉到项目内部（web/services/auto_runner.py），本任务只负责开机拉起。本机账户无密码，开机后约 12 秒自动登录，故等同开机自启；浏览器抓取依赖该桌面会话，不能在登录前运行。' | Out-Null

Write-Output "任务已注册：$taskName"
$t = Get-ScheduledTask -TaskName $taskName
$i = Get-ScheduledTaskInfo -TaskName $taskName
Write-Output ("状态      : {0}" -f $t.State)
Write-Output ("运行身份  : {0} / 提权级别 {1}" -f $t.Principal.UserId, $t.Principal.RunLevel)
Write-Output ("动作      : {0}" -f $t.Actions[0].Arguments)
Write-Output ("触发      : {0}" -f ($t.Triggers | ForEach-Object { $_.CimClass.CimClassName }))
Write-Output ("上次结果  : {0}" -f $i.LastTaskResult)