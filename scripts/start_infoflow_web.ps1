# InfoFlowHub 一键启动：Web + Tunnel + Chrome 书签同步到手机
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir
Set-Location $root

$port = 18421
$bindHost = "127.0.0.1"
$localUrl = "http://${bindHost}:${port}/"
$chromeScript = Join-Path $scriptDir "update_chrome_bookmark.py"
$chromeExe = "C:\Program Files\Google\Chrome\Application\chrome.exe"
$cloudflaredExe = Join-Path $scriptDir "cloudflared.exe"
$logDir = Join-Path $root "runtime"
$logPath = Join-Path $logDir "web.log"
$pyExe = "$env:USERPROFILE\.workbuddy\binaries\python\versions\3.13.12\python.exe"

$python = $env:INFOFLOW_PYTHON
if ([string]::IsNullOrWhiteSpace($python)) { $python = "python" }

if (!(Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

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

$command = "Set-Location '$root'; & '$python' -m uvicorn web.app:app --host $bindHost --port $port *>> '$logPath'"
Start-Process powershell -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-Command", $command -WindowStyle Hidden

$deadline = (Get-Date).AddSeconds(45)
$webReady = $false
do {
    Start-Sleep -Seconds 1
    try {
        $resp = Invoke-WebRequest -Uri $localUrl -UseBasicParsing -TimeoutSec 3
        if ($resp.StatusCode -eq 200 -and $resp.Content -match "InfoFlowHub") {
            $webReady = $true
            break
        }
    } catch {}
} while ((Get-Date) -lt $deadline)

if (-not $webReady) { throw "Web service failed to start within 45s: $localUrl" }

Write-Host "[OK] Web service running at $localUrl" -ForegroundColor Green

# ============================================================
# Phase 3: Tunnel + Chrome bookmark
# ============================================================
Write-Host "=== Phase 3: Tunnel + Chrome bookmark ===" -ForegroundColor Cyan

# 3a. Close Chrome to unlock bookmarks DB
Write-Host "[Step] Closing Chrome..." -ForegroundColor Cyan
Get-Process -Name "chrome" -ErrorAction SilentlyContinue | Stop-Process -Force

$chromeBookmarks = "$env:LOCALAPPDATA\Google\Chrome\User Data\Default\Bookmarks"
$deadline = (Get-Date).AddSeconds(20)
while ($true) {
    try {
        $fs = [System.IO.File]::Open($chromeBookmarks, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
        $fs.Close()
        break
    } catch {
        if ((Get-Date) -gt $deadline) {
            Write-Host "[WARN] Chrome bookmarks still locked after 20s, proceeding anyway" -ForegroundColor Yellow
            break
        }
        Start-Sleep -Milliseconds 500
    }
}
Write-Host "[OK] Chrome closed" -ForegroundColor Green

# 3b. Start cloudflared
$pinfo = New-Object System.Diagnostics.ProcessStartInfo
$pinfo.FileName = $cloudflaredExe
$pinfo.Arguments = "tunnel --url http://localhost:${port}"
$pinfo.RedirectStandardOutput = $true
$pinfo.RedirectStandardError = $true
$pinfo.UseShellExecute = $false
$pinfo.CreateNoWindow = $true

$cf = New-Object System.Diagnostics.Process
$cf.StartInfo = $pinfo
$cf.Start() | Out-Null

$sw = [System.Diagnostics.Stopwatch]::StartNew()
$tunnelUrl = $null
while ($cf.HasExited -eq $false -and $sw.Elapsed.TotalSeconds -lt 30) {
    $line = $cf.StandardError.ReadLine()
    if ($line -match "https://(.+?)\.trycloudflare\.com") {
        $tunnelUrl = $matches[0]
        break
    }
}
$cf.Dispose()

if (-not $tunnelUrl) {
    Write-Host "[WARN] Failed to get tunnel URL, skipping bookmark update" -ForegroundColor Yellow
} else {
    Write-Host "[OK] Tunnel URL: $tunnelUrl" -ForegroundColor Green

    # 3c. Update Chrome bookmark
    Write-Host "[Step] Updating Chrome bookmark..." -ForegroundColor Cyan
    $cr = & $pyExe $chromeScript $tunnelUrl 2>&1
    if ($cr -like "CREATED*" -or $cr -like "UPDATED*") {
        Write-Host "[OK] Chrome bookmark written" -ForegroundColor Green
    } elseif ($cr -like "UNCHANGED*") {
        Write-Host "[OK] Chrome bookmark already up to date" -ForegroundColor Green
    } else {
        Write-Host "[WARN] Chrome: $cr" -ForegroundColor Yellow
    }
}

# ============================================================
# Phase 4: Open Chrome
# ============================================================
Write-Host "=== Phase 4: Open Chrome ===" -ForegroundColor Cyan

if ($tunnelUrl -and (Test-Path $chromeExe)) {
    Start-Process $chromeExe -ArgumentList $tunnelUrl | Out-Null
    Write-Host "[OK] Chrome opened - sync will push bookmark to phone" -ForegroundColor Green
} else {
    Start-Process $localUrl | Out-Null
}

Write-Host "=== All done ===" -ForegroundColor Green
