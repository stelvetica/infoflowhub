# 定期检查 bsk（BrowserSkill）更新与版本错配，结果追加到 runtime\bsk_update_check.log。
#
# 退出码约定（供计划任务「上次运行结果」判读）：
#   0 = 全部最新：CLI 已是最新，且无 CLI/扩展版本错配
#   2 = 有更新可用，或存在 CLI/扩展版本错配（daemon 状态未知时错配未知，不据此判 2）
#   1 = 检查本身失败（如 bsk 不在 PATH、update 子命令无有效 JSON）
#
# 注意：`bsk update` 只管 CLI/daemon；浏览器扩展走扩展自己的更新渠道，CLI 无法更新它，
# 故扩展版本错配只能提示用户手动更新扩展。
#
# 不挂起：拿不到 daemon 状态时（没在跑、或权限级别低于 daemon 导致管道拒绝访问）
# 这里优雅降级（仍报 CLI 检查结果，并如实写明失败原因），不会因此整体失败。
$ErrorActionPreference = 'Stop'

$logPath = Join-Path (Split-Path $PSScriptRoot -Parent) 'runtime\bsk_update_check.log'
$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'

function Resolve-Bsk {
    $cmd = Get-Command bsk -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($cand in @(
        (Join-Path $env:USERPROFILE '.local\bin\bsk.exe'),
        (Join-Path $env:USERPROFILE '.local\bin\bsk.cmd'),
        (Join-Path $env:USERPROFILE '.local\bin\bsk')
    )) {
        if (Test-Path $cand) { return $cand }
    }
    return $null
}

function Write-Line([string]$text) {
    $dir = Split-Path $logPath -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $line = "[$stamp] $text"
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::AppendAllText($logPath, $line + [Environment]::NewLine, $utf8NoBom)
    Write-Host $line
}

$bsk = Resolve-Bsk
if (-not $bsk) {
    Write-Line 'FAIL 未找到 bsk CLI（不在 PATH），请先安装 browser-skill。'
    exit 1
}

# 检查阶段绝不让 bsk 顺手拉起 daemon。
$env:BSK_AUTO_START = '0'

# 1) CLI 更新检查（bsk update 只管 CLI/daemon，不覆盖扩展）。
$update = $null
try {
    $raw = (& $bsk update --check --json 2>$null) -join "`n"
    if ($raw) { $update = $raw | ConvertFrom-Json }
} catch { $update = $null }
if (-not $update -or -not $update.status) {
    Write-Line 'FAIL bsk update --check 未返回有效 JSON，检查失败。'
    exit 1
}

$cliCurrent = [string]$update.current_version
$cliLatest = [string]$update.latest_version
$cliStatus = [string]$update.status
$releaseUrl = [string]$update.release_url

# 2) daemon / 扩展版本：拿不到就优雅降级（仍报 CLI 结论），并如实写明失败原因。
#    注意：若原因是「os error 5 拒绝访问」，那不是 daemon 没跑，而是本任务的权限级别
#    低于 daemon（daemon 由 Highest 任务启动，其命名管道拒绝 LIMITED 客户端）。
$status = $null
$statusErr = ''
try {
    $raw = (& $bsk status --json 2>$null) -join "`n"
    if ($raw) { $status = $raw | ConvertFrom-Json }
    if (-not $status) { $statusErr = ($raw -replace '\s+', ' ') }
} catch { $statusErr = $_.Exception.Message }

$parts = @()
$updateAvailable = ($cliStatus -ne 'up_to_date')
$parts += "CLI $cliCurrent -> $cliLatest ($cliStatus)"
if ($updateAvailable -and $releaseUrl) { $parts += "release=$releaseUrl" }

$hasSkew = $false
if ($status -and $status.daemon_version) {
    $parts += "daemon $($status.daemon_version)"
    $extVersions = @()
    foreach ($b in @($status.browsers)) {
        if ($b -and $b.extension_version) { $extVersions += "$($b.browser_name) $($b.extension_version)" }
    }
    if ($extVersions.Count -gt 0) { $parts += '扩展 ' + ($extVersions -join ', ') }
    else { $parts += '扩展 无已连接浏览器' }
    if (@($status.version_skew_browsers).Count -gt 0) {
        $hasSkew = $true
        $parts += '版本错配 ' + (@($status.version_skew_browsers) -join ', ')
    }
    else { $parts += '无版本错配' }
}
else {
    $reason = if ($statusErr) { ($statusErr.Substring(0, [Math]::Min(110, $statusErr.Length))) } else { '原因未知' }
    $parts += "daemon 状态未知（bsk status 失败：$reason）"
}

Write-Line ($parts -join ' | ')

if ($updateAvailable -or $hasSkew) {
    if ($hasSkew) {
        Write-Line '提示：扩展走浏览器扩展渠道更新，bsk CLI 无法更新扩展；请手动更新扩展。'
    }
    exit 2
}
exit 0