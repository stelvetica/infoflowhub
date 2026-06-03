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
$pyExe = "C:\Users\TB14Plus\.workbuddy\binaries\python\versions\3.13.12\python.exe"

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
$deadline = (Get-Date).AddSeconds(30)
while ($true) {
    try {
        $fs = [System.IO.File]::Open($chromeBookmarks, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
        $fs.Close()
        break
    } catch {
        if ((Get-Date) -gt $deadline) {
            Write-Host "[WARN] Chrome bookmarks still locked after 30s, proceeding anyway" -ForegroundColor Yellow
            break
        }
        Start-Sleep -Seconds 3
    }
}
Write-Host "[OK] Chrome closed" -ForegroundColor Green

# 3b. Start cloudflared
$tunnelUrl = $null
$cfLog = Join-Path $logDir "cloudflared.log"

$cfProc = Start-Process -FilePath $cloudflaredExe -ArgumentList "tunnel","--url","http://127.0.0.1:${port}","--no-autoupdate" -NoNewWindow -PassThru -RedirectStandardError $cfLog
$sw = [System.Diagnostics.Stopwatch]::StartNew()
while ($sw.Elapsed.TotalSeconds -lt 30) {
    Start-Sleep -Milliseconds 500
    if (Test-Path $cfLog) {
        $content = Get-Content $cfLog -Raw -ErrorAction SilentlyContinue
        if ($content -match "https://(.+?)\.trycloudflare\.com") {
            $tunnelUrl = $matches[0]
            break
        }
    }
    if ($cfProc.HasExited) { break }
}

if (-not $tunnelUrl) {
    Write-Host "[WARN] Failed to get tunnel URL, skipping bookmark update" -ForegroundColor Yellow
} else {
    Write-Host "[OK] Tunnel URL: $tunnelUrl" -ForegroundColor Green

    # 3c. Update Chrome bookmark directly via PowerShell
    Write-Host "[Step] Updating Chrome bookmark..." -ForegroundColor Cyan
    $bmPath = "$env:LOCALAPPDATA\Google\Chrome\User Data\Default\Bookmarks"
    $tunnelUrlNormalized = $tunnelUrl.TrimEnd("/")
    try {
        $bmData = Get-Content $bmPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $bar = $bmData.roots.bookmark_bar
        $existing = $bar.children | Where-Object { $_.name -eq "InfoFlowHub" }
        if ($existing) {
            if ($existing.url -eq $tunnelUrlNormalized) {
                Write-Host "[OK] Chrome bookmark already up to date" -ForegroundColor Green
            } else {
                $existing.url = $tunnelUrlNormalized
                Write-Host "[OK] Chrome bookmark updated" -ForegroundColor Green
            }
        } else {
            $newBm = [PSCustomObject]@{
                date_added = [string]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() * 1000)
                date_last_used = "0"
                guid = "infoflowhub-auto-tunnel-001"
                id = "9999"
                meta_info = [PSCustomObject]@{ power_bookmark_meta = "" }
                name = "InfoFlowHub"
                type = "url"
                url = $tunnelUrlNormalized
            }
            $bar.children = @($newBm) + $bar.children
            Write-Host "[OK] Chrome bookmark created" -ForegroundColor Green
        }
        # Recalculate checksum
        $bmData.PSObject.Properties.Remove("checksum")
        $utf8NoBom = New-Object System.Text.UTF8Encoding $false
        $rawBytes = $utf8NoBom.GetBytes(($bmData | ConvertTo-Json -Depth 10 -Compress))
        $md5 = [System.Security.Cryptography.MD5]::Create()
        $hash = [BitConverter]::ToString($md5.ComputeHash($rawBytes)).Replace("-", "").ToLower()
        $bmData | Add-Member -NotePropertyName "checksum" -NotePropertyValue $hash -Force
        $bmData | ConvertTo-Json -Depth 10 | Set-Content $bmPath -Encoding UTF8
    } catch {
        Write-Host "[WARN] Chrome bookmark update failed: $_" -ForegroundColor Yellow
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
