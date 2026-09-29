# InfoFlowHub 开机自启入口：Web 服务 + bsk daemon（供计划任务在登录时调用）
#
# 为什么触发点是"登录时"而非"启动时"：浏览器抓取依赖用户桌面会话里的 Chrome 与
# bsk 扩展，登录前（session 0）没有浏览器，daemon 起了也连不上扩展。本机账户无密码，
# 开机后约 12 秒自动进入桌面会话，故效果等同"开机即跑"，无需人工操作。
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir
$port = 18421
$localUrl = "http://127.0.0.1:$port/"
$logPath = Join-Path $root "runtime\autostart.log"

# 无人值守启动：关键决策点落盘，否则任务失败时无从排查
function Write-Log([string]$Message) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    Add-Content -Path $logPath -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue
}

function Test-WebServiceUp {
    try {
        $resp = Invoke-WebRequest -Uri $localUrl -UseBasicParsing -TimeoutSec 5
        return ($resp.StatusCode -eq 200 -and $resp.Content -match "InfoFlowHub")
    } catch { return $false }
}

# bsk daemon 探活：daemon 可能已被抓取侧的 BskBrowser.ensure_daemon() 拉起。看护触发器每 5 分钟
# 跑一次本脚本，若无脑 `bsk daemon start --foreground` 会撞上已存在的 daemon 并失败退出，进而触发
# 任务 RestartOnFailure 每分钟重试 3 次的无谓抖动。故先探活，已在跑就跳过、绝不触碰。
#
# 必须带 BSK_AUTO_START=0：不带这个变量时 `bsk status` 会顺手把 daemon 拉起来（自动启动副作用），
# 那就失去了"只探活"的意义。finally 里清理，避免污染同脚本内后续的 bsk 调用。
function Test-BskDaemonUp {
    $env:BSK_AUTO_START = '0'
    try { & $bsk status --json *> $null; return ($LASTEXITCODE -eq 0) }
    finally { Remove-Item Env:BSK_AUTO_START -ErrorAction SilentlyContinue }
}

# 1) Web 服务：已在跑就绝不触碰。
#    start_infoflow_web.ps1 的 Phase 1 会强杀 18421 端口进程与 cloudflared，
#    无脑调用会打断健康实例和隧道；只在探活失败时才拉起。
if (Test-WebServiceUp) {
    Write-Log "[skip] Web 服务已在运行: $localUrl"
} else {
    Write-Log "[start] Web 服务未响应，拉起中..."
    & (Join-Path $scriptDir "start_infoflow_web.ps1") -NoBrowser
    Write-Log "[ok] Web 服务已就绪"
}

# 2) bsk daemon：已在跑就绝不触碰；未跑才前台运行，让计划任务持有该进程（崩溃由任务重启策略兜住）。
$bsk = Join-Path $env:USERPROFILE ".local\bin\bsk.exe"
if (-not (Test-Path $bsk)) { throw "找不到 bsk.exe: $bsk" }

if (Test-BskDaemonUp) {
    Write-Log "[skip] bsk daemon 已在运行"
} else {
    Write-Log "[start] bsk daemon（前台，由本任务持有）"
    & $bsk daemon start --foreground
    Write-Log "[exit] bsk daemon 已退出（任务将按重启策略处理）"
}