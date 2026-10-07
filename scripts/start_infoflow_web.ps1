# InfoFlowHub 一键启动：Web 服务（仅本地）
# 隧道（cloudflared + Chrome 书签同步）见 start_tunnel.ps1
# -NoBrowser：跳过 Phase 3 的打开浏览器，供计划任务自启使用（避免每次登录弹窗）
param([switch]$NoBrowser)

$ErrorActionPreference = "Stop"
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir
Set-Location $root

$port = 18421
$bindHost = "127.0.0.1"
$localUrl = "http://${bindHost}:${port}/"
$logDir = Join-Path $root "runtime"
$logPath = Join-Path $logDir "web.log"
$errPath = Join-Path $logDir "web.err.log"

if (!(Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

# ============================================================
# Phase 0: Ensure Python env (machine-independent)
# ============================================================
Write-Host "=== Phase 0: Ensuring Python environment ===" -ForegroundColor Cyan
$python = & (Join-Path $scriptDir "ensure_python_env.ps1")
if ([string]::IsNullOrWhiteSpace($python)) { throw "No usable Python interpreter resolved." }
Write-Host "[OK] Python: $python" -ForegroundColor Green

# ============================================================
# Phase 1: Stop old processes
# ============================================================
Write-Host "=== Phase 1: Cleaning up old processes ===" -ForegroundColor Cyan

$lines = netstat -ano -p tcp | Select-String ":$port"
$pids = @()
foreach ($line in $lines) {
    $parts = ($line.ToString() -replace "\s+", " ").Trim().Split(" ")
    if ($parts.Length -ge 5) {
        $targetPid = 0
        if ([int]::TryParse($parts[-1], [ref]$targetPid) -and $targetPid -gt 0) {
            $pids += $targetPid
        }
    }
}
$pids | Select-Object -Unique | ForEach-Object {
    try { Stop-Process -Id $_ -Force -ErrorAction Stop } catch {}
}

Get-CimInstance Win32_Process -Filter "name = 'python.exe' or name = 'pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*$root*" -and $_.CommandLine -like "*uvicorn*" } |
    ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }

$deadline = (Get-Date).AddSeconds(15)
do {
    $still = netstat -ano -p tcp | Select-String "LISTENING" | Select-String ":$port"
    if (-not $still) { break }
    Start-Sleep -Milliseconds 500
} while ((Get-Date) -lt $deadline)

if ($still) { throw "Port $port was not released in time." }

Get-Process -Name "cloudflared" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

Write-Host "[OK] Old processes cleaned" -ForegroundColor Green

# ============================================================
# Phase 2: Start Web service
# ============================================================
Write-Host "=== Phase 2: Starting Web service ===" -ForegroundColor Cyan

# 输出重定向必须走 Start-Process 的原生句柄，不能用内层 powershell + *>>：
# PS 流层会把原生子进程 stderr 按 UTF-16 写盘，与 Python 直写的 stdout 字节混成
# 一个文件两种编码，[bsk]/uvicorn 日志因此乱码不可 grep（2026-10-07 排障实录）。
# 句柄直写后编码由 python -X utf8 统一为 UTF-8，-u 关掉 stdout 块缓冲保证实时。
# 分工：web.log = 业务流程 stdout（laterhub 打印、[bilibili_auth]）；web.err.log =
# 服务与诊断 stderr（uvicorn 日志、错误栈、[bsk] 命令日志）——排障先看后者。
# Start-Process 重定向为截断写（不支持追加）：日志以「本次启动」为视角，重启即清零。
Start-Process -FilePath $python `
    -ArgumentList "-X", "utf8", "-u", "-m", "uvicorn", "web.app:app", "--host", $bindHost, "--port", "$port" `
    -WorkingDirectory $root `
    -RedirectStandardOutput $logPath `
    -RedirectStandardError $errPath `
    -WindowStyle Hidden

$deadline = (Get-Date).AddSeconds(45)
$webReady = $false
do {
    Start-Sleep -Seconds 1
    try {
        $resp = Invoke-WebRequest -Uri $localUrl -UseBasicParsing -TimeoutSec 10
        if ($resp.StatusCode -eq 200 -and $resp.Content -match "InfoFlowHub") {
            $webReady = $true
            break
        }
    } catch {}
} while ((Get-Date) -lt $deadline)

if (-not $webReady) { throw "Web service failed to start within 45s: $localUrl (启动报错见 runtime\web.err.log 末尾)" }

Write-Host "[OK] Web service running at $localUrl" -ForegroundColor Green

# ============================================================
# Phase 3: Open browser
# ============================================================
if ($NoBrowser) {
    Write-Host "[skip] Phase 3: browser not opened (-NoBrowser)" -ForegroundColor DarkGray
} else {
    Write-Host "=== Phase 3: Open browser ===" -ForegroundColor Cyan
    Start-Process $localUrl | Out-Null
    Write-Host "[OK] Default browser opened (local URL)" -ForegroundColor Green
}

Write-Host "=== All done ===" -ForegroundColor Green
Write-Host "Local: $localUrl" -ForegroundColor Green
Write-Host "Need remote access? Run: scripts\start_tunnel.ps1" -ForegroundColor DarkGray
