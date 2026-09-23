$ErrorActionPreference = 'Stop'

$bsk = 'C:\Users\ETT\.local\bin\bsk.exe'
if (-not (Test-Path $bsk)) { throw "找不到 bsk.exe: $bsk" }

$ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$taskName = 'bsk daemon (BrowserSkill)'

# 已有同名任务先移除，保证幂等
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Output "已移除旧任务"
}

$action = New-ScheduledTaskAction -Execute $ps `
    -Argument "-NoProfile -WindowStyle Hidden -Command `"& '$bsk' daemon start --foreground`""

$user = "$env:USERDOMAIN\$env:USERNAME"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

# 必须用当前用户身份（SYSTEM 会话里没有用户的 Chrome 与扩展），且不需要提权
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName `
    -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description '常驻 bsk daemon（browser-skill）：供 infoflowhub 定时抓取复用日常 Chrome 登录态。随登录启动，崩溃自动重启，不限运行时长的前台服务进程。' | Out-Null

Write-Output "任务已注册：$taskName"
$t = Get-ScheduledTask -TaskName $taskName
$i = Get-ScheduledTaskInfo -TaskName $taskName
Write-Output ("状态      : {0}" -f $t.State)
Write-Output ("运行身份  : {0}" -f $t.Principal.UserId)
Write-Output ("提权级别  : {0}" -f $t.Principal.RunLevel)
Write-Output ("动作      : {0} {1}" -f $t.Actions[0].Execute, $t.Actions[0].Arguments)
Write-Output ("触发      : {0}" -f ($t.Triggers | ForEach-Object { $_.CimClass.CimClassName }))
Write-Output ("下次运行  : {0}" -f $i.NextRunTime)