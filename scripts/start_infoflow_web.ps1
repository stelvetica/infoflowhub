# InfoFlowHub 一键启动：Web 服务（仅本地）
# 隧道（cloudflared + Chrome 书签同步）见 start_tunnel.ps1
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
# Phase 1.5: Ensure wechat-mp-tools (公众号外部工具, port 5300)
# 微信 9 源走 weread:// 轻量协议，依赖该工具；已运行则不重复拉起。
# ============================================================
Write-Host "=== Phase 1.5: Ensuring wechat-mp-tools (port 5300) ===" -ForegroundColor Cyan

$mpToolDir = Join-Path $root "wechat-mp-tools"
$mpToolPort = 5300
$mpToolUp = netstat -ano -p tcp | Select-String "LISTENING" | Select-String ":$mpToolPort"
if ($mpToolUp) {
    Write-Host "[OK] wechat-mp-tools already running on $mpToolPort" -ForegroundColor Green
} elseif ((Test-Path $mpToolDir) -and (Test-Path (Join-Path $mpToolDir "venv312\Scripts\python.exe"))) {
    $mpToolPython = Join-Path $mpToolDir "venv312\Scripts\python.exe"
    $mpLog = Join-Path $logDir "mp_tools.log"
    $mpCommand = "Set-Location '$mpToolDir'; & '$mpToolPython' app.py --port $mpToolPort --no-browser *>> '$mpLog'"
    Start-Process powershell -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-Command", $mpCommand -WindowStyle Hidden
    $deadline = (Get-Date).AddSeconds(30)
    do {
        Start-Sleep -Seconds 1
        $mpToolUp = netstat -ano -p tcp | Select-String "LISTENING" | Select-String ":$mpToolPort"
    } while ((-not $mpToolUp) -and ((Get-Date) -lt $deadline))
    if ($mpToolUp) { Write-Host "[OK] wechat-mp-tools running at http://127.0.0.1:$mpToolPort" -ForegroundColor Green }
    else { Write-Host "[WARN] wechat-mp-tools 30s 内未监听 $mpToolPort，公众号源将不可用（详见 runtime\mp_tools.log）" -ForegroundColor Yellow }
} else {
    Write-Host "[WARN] wechat-mp-tools 或其 venv312 缺失，跳过拉起，公众号源将不可用" -ForegroundColor Yellow
}

# ============================================================
# Phase 2: Start Web service
# ============================================================
Write-Host "=== Phase 2: Starting Web service ===" -ForegroundColor Cyan

$command = "Set-Location '$root'; & '$python' -m uvicorn web.app:app --host $bindHost --port $port *>> '$logPath'"
Start-Process powershell -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-Command", $command -WindowStyle Hidden

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

if (-not $webReady) { throw "Web service failed to start within 45s: $localUrl" }

Write-Host "[OK] Web service running at $localUrl" -ForegroundColor Green

# ============================================================
# Phase 3: Open browser
# ============================================================
Write-Host "=== Phase 3: Open browser ===" -ForegroundColor Cyan

Start-Process $localUrl | Out-Null
Write-Host "[OK] Default browser opened (local URL)" -ForegroundColor Green

Write-Host "=== All done ===" -ForegroundColor Green
Write-Host "Local: $localUrl" -ForegroundColor Green
Write-Host "Need remote access? Run: scripts\start_tunnel.ps1" -ForegroundColor DarkGray
