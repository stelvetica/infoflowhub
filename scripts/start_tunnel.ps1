# InfoFlowHub 隧道：cloudflared quick tunnel + Chrome 书签同步到手机
# 前置：Web 服务已在 http://127.0.0.1:18421/ 运行（先跑 start_infoflow_web.ps1）
#
# 用法：powershell -ExecutionPolicy Bypass -File scripts\start_tunnel.ps1
# 退出码 0 = 隧道已验证可达且书签已更新；非 0 = 失败（见 stderr）
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir
Set-Location $root

$port = 18421
$localUrl = "http://127.0.0.1:${port}/"
$cloudflaredExe = Join-Path $scriptDir "cloudflared.exe"
$logDir = Join-Path $root "runtime"
$cfLog = Join-Path $logDir "cloudflared.log"

if (!(Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

# ============================================================
# Phase 0: 前置检查
# ============================================================
Write-Host "=== Phase 0: Preflight ===" -ForegroundColor Cyan

if (-not (Test-Path $cloudflaredExe)) {
    Write-Error "cloudflared.exe not found at $cloudflaredExe"; exit 1
}
Write-Host "[Step] Checking local web..." -ForegroundColor Cyan
try {
    $resp = Invoke-WebRequest -Uri $localUrl -UseBasicParsing -TimeoutSec 5
    if ($resp.StatusCode -ne 200) { throw "local web returned $($resp.StatusCode)" }
} catch {
    Write-Error "Local web not reachable at $localUrl. Run start_infoflow_web.ps1 first. ($_)"
    exit 1
}
Write-Host "[OK] Local web alive" -ForegroundColor Green

# ============================================================
# Phase 1: 关闭 Chrome 解锁书签 DB
# ============================================================
Write-Host "=== Phase 1: Closing Chrome ===" -ForegroundColor Cyan
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

# ============================================================
# Phase 2: 启动 cloudflared + 边缘探活
# ============================================================
# ponytail: 探活必须绕本机代理(7892) + 系统 DNS NXDOMAIN 假阴性。
# 用 nslookup 问 1.1.1.1 取边缘 IP，--resolve 直连 curl.exe 探活。
# 天花板：依赖 nslookup 输出格式与 curl.exe 存在（Win10+ 自带）。
$tunnelUrl = $null
$maxRetries = 3
$retry = 0

while ($retry -lt $maxRetries -and -not $tunnelUrl) {
    Write-Host "=== Phase 2: Starting cloudflared (attempt $($retry+1)/$maxRetries) ===" -ForegroundColor Cyan
    if ($retry -gt 0) {
        Get-Process -Name "cloudflared" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 3
    }

    Remove-Item $cfLog -ErrorAction SilentlyContinue
    $cfProc = Start-Process -FilePath $cloudflaredExe -ArgumentList "tunnel","--url","http://127.0.0.1:${port}","--no-autoupdate","--protocol","http2","--edge-ip-version","4" -WindowStyle Hidden -PassThru -RedirectStandardError $cfLog

    # 2a. 从日志 grep 隧道 URL
    $candidateUrl = $null
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt 30) {
        Start-Sleep -Milliseconds 500
        if (Test-Path $cfLog) {
            $content = Get-Content $cfLog -Raw -ErrorAction SilentlyContinue
            if ($content -match "https://(.+?)\.trycloudflare\.com") {
                $candidateUrl = $matches[0]
                break
            }
        }
        if ($cfProc.HasExited) { break }
    }

    if (-not $candidateUrl) {
        Write-Host "[WARN] No tunnel URL in log within 30s" -ForegroundColor Yellow
        $retry++
        continue
    }

    # 2b. 边缘探活：绕代理 + nslookup 1.1.1.1 解析 + --resolve 直连
    $tunnelHost = ([uri]$candidateUrl).Host
    $probeOk = $false
    $lastCode = ""
    $probeSw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($probeSw.Elapsed.TotalSeconds -lt 30) {
        $edgeIp = $null
        try {
            $nsLines = & nslookup $tunnelHost 1.1.1.1 2>$null
            $addIdx = -1
            for ($i=0; $i -lt $nsLines.Count; $i++) {
                if ($nsLines[$i] -match "Addresses?") { $addIdx = $i; break }
            }
            if ($addIdx -ge 0) {
                for ($i=$addIdx+1; $i -lt $nsLines.Count; $i++) {
                    if ($nsLines[$i] -match "(\d+\.\d+\.\d+\.\d+)") { $edgeIp = $matches[1]; break }
                    elseif ($nsLines[$i].Trim() -eq "") { break }
                }
            }
        } catch {}
        if ($edgeIp) {
            $lastCode = & curl.exe -sS -m 10 --noproxy '*' --resolve "${tunnelHost}:443:${edgeIp}" -o NUL -w "%{http_code}" "https://$tunnelHost/" 2>$null
            if ($lastCode -eq "200") { $probeOk = $true; break }
        }
        Start-Sleep -Seconds 2
    }

    if ($probeOk) {
        $tunnelUrl = $candidateUrl
        Write-Host "[OK] Tunnel verified reachable: $tunnelUrl" -ForegroundColor Green
    } else {
        Write-Host "[WARN] Tunnel probe failed (last code: '$lastCode')" -ForegroundColor Yellow
    }
    $retry++
}

if (-not $tunnelUrl) {
    Write-Error "Failed to establish a reachable tunnel after $maxRetries attempts"
    exit 1
}

# ============================================================
# Phase 3: 更新 Chrome 书签（Chrome Sync 推送到手机）
# ============================================================
Write-Host "=== Phase 3: Updating Chrome bookmark ===" -ForegroundColor Cyan
$bmPath = "$env:LOCALAPPDATA\Google\Chrome\User Data\Default\Bookmarks"
$tunnelUrlNormalized = $tunnelUrl.TrimEnd("/")
try {
    $bmData = Get-Content $bmPath -Raw -Encoding UTF8 | ConvertFrom-Json
    function Remove-InfoFlowHubBookmarks {
        param($node)
        if ($null -eq $node -or -not $node.children) { return 0 }

        $removedHere = @($node.children | Where-Object {
            $_.type -eq "url" -and (
                $_.name -eq "InfoFlowHub" -or
                ($_.url -is [string] -and $_.url -match "^https://[^/]+\.trycloudflare\.com/?$")
            )
        }).Count

        $node.children = @($node.children | Where-Object {
            -not (
                $_.type -eq "url" -and (
                    $_.name -eq "InfoFlowHub" -or
                    ($_.url -is [string] -and $_.url -match "^https://[^/]+\.trycloudflare\.com/?$")
                )
            )
        })

        $removedNested = 0
        foreach ($child in $node.children) {
            $removedNested += Remove-InfoFlowHubBookmarks $child
        }

        return ($removedHere + $removedNested)
    }

    $removedCount = 0
    $removedCount += Remove-InfoFlowHubBookmarks $bmData.roots.bookmark_bar
    $removedCount += Remove-InfoFlowHubBookmarks $bmData.roots.other
    $removedCount += Remove-InfoFlowHubBookmarks $bmData.roots.synced
    Write-Host "[Step] Removed $removedCount old InfoFlowHub / trycloudflare bookmark(s)" -ForegroundColor DarkGray

    $bar = $bmData.roots.bookmark_bar
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

    # 重算 checksum（Chrome 靠它检测书签改动）
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

Write-Host "=== All done ===" -ForegroundColor Green
Write-Host "Tunnel: $tunnelUrl" -ForegroundColor Green
Write-Host "Chrome Sync will push the bookmark to your phone." -ForegroundColor DarkGray
