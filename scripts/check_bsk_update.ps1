# 定期检查 bsk（BrowserSkill）版本一致性，必要时把 CLI 追平扩展；结果追加到
# runtime\bsk_update_check.log。
#
# 为什么要「追平」这一档：浏览器扩展走扩展自己的更新渠道，会在后台静默升级；CLI 不会跟着动
# （`bsk update` 也管不到扩展）。两者由上游成对发布、协议需一致，长期错开迟早出问题。故本脚本
# 检测到**扩展版本高于 CLI** 时，直接 `bsk update -y` 把 CLI 升到最新（≥ 扩展版本）。
# 反方向（CLI 高于扩展）无解：CLI 无法更新扩展，只能等扩展自己升级，故只提示、不动作。
#
# 退出码约定（供调用方判读）：
#   0 = 全部最新：CLI 已是最新，且无 CLI/扩展版本错配
#   2 = 仍有待处理项：有更新可用、存在版本错配，或扩展仍领先 CLI（自动追平未成功）
#   1 = 检查本身失败（如 bsk 不在 PATH、update 子命令无有效 JSON）
#
# 注意：`bsk update` 只管 CLI/daemon；浏览器扩展走扩展自己的更新渠道，CLI 无法更新它。
#
# 不挂起：拿不到 daemon 状态时（没在跑、或权限级别低于 daemon 导致管道拒绝访问）
# 这里优雅降级（仍报 CLI 检查结果，并如实写明失败原因），不会因此整体失败。
$ErrorActionPreference = 'Stop'

$logPath = Join-Path (Split-Path $PSScriptRoot -Parent) 'runtime\bsk_update_check.log'
$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
# 升级后 daemon 会重启，status 需要一点时间才重新可用（官方建议最多 5 次、每次隔 1s）。
$STATUS_RETRIES_AFTER_UPGRADE = 5

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

function ConvertTo-BskVersion([string]$text) {
    # "0.3.2" / "v0.3.2" -> [version]；解析不了返回 $null，调用方据此跳过版本比较
    # （宁可只报告，也不拿解析失败的字符串去做升级决定）。
    if (-not $text) { return $null }
    $clean = $text.Trim() -replace '^v', ''
    if (-not $clean) { return $null }
    try { return [version]$clean } catch { return $null }
}

function Get-BskUpdateInfo {
    param([string]$Bsk)
    try {
        $raw = (& $Bsk update --check --json 2>$null) -join "`n"
        if ($raw) { return ($raw | ConvertFrom-Json) }
    } catch { return $null }
    return $null
}

function Get-BskStatusInfo {
    param([string]$Bsk, [int]$Retries = 1)
    $lastErr = ''
    for ($i = 1; $i -le $Retries; $i++) {
        if ($i -gt 1) { Start-Sleep -Seconds 1 }
        try {
            $raw = (& $Bsk status --json 2>$null) -join "`n"
            if ($raw) {
                $data = $raw | ConvertFrom-Json
                if ($data) { return @{ Data = $data; Err = '' } }
            }
            $lastErr = ($raw -replace '\s+', ' ')
        } catch { $lastErr = $_.Exception.Message }
    }
    return @{ Data = $null; Err = $lastErr }
}

function Get-NewestExtensionVersion {
    # 多浏览器同时连着时取最"新"的一个：只要有一个扩展领先，CLI 就得跟上它。
    param($Status)
    $newest = $null
    $text = ''
    if (-not $Status) { return @{ Version = $null; Text = '' } }
    foreach ($b in @($Status.browsers)) {
        if (-not $b -or -not $b.extension_version) { continue }
        $v = ConvertTo-BskVersion ([string]$b.extension_version)
        if ($v -and (-not $newest -or $v -gt $newest)) { $newest = $v; $text = [string]$b.extension_version }
    }
    return @{ Version = $newest; Text = $text }
}

$bsk = Resolve-Bsk
if (-not $bsk) {
    Write-Line 'FAIL 未找到 bsk CLI（不在 PATH），请先安装 browser-skill。'
    exit 1
}

# 检查阶段绝不让 bsk 顺手拉起 daemon。
$env:BSK_AUTO_START = '0'

# 1) CLI 更新检查（bsk update 只管 CLI/daemon，不覆盖扩展）。
$update = Get-BskUpdateInfo $bsk
if (-not $update -or -not $update.status) {
    Write-Line 'FAIL bsk update --check 未返回有效 JSON，检查失败。'
    exit 1
}

# 2) daemon / 扩展版本：拿不到就优雅降级（仍报 CLI 结论），并如实写明失败原因。
#    注意：若原因是「os error 5 拒绝访问」，那不是 daemon 没跑，而是调用方权限级别
#    低于 daemon（daemon 由 Highest 任务启动，其命名管道拒绝 LIMITED 客户端）。
$probe = Get-BskStatusInfo $bsk
$status = $probe.Data
$statusErr = $probe.Err

# 3) 扩展领先就把 CLI 追平（本脚本唯一的写动作，只在真需要时发生）。
$ext = Get-NewestExtensionVersion $status
$cliVer = ConvertTo-BskVersion ([string]$update.current_version)
$extAhead = ($ext.Version -and $cliVer -and $ext.Version -gt $cliVer)
$skewed = @($status.version_skew_browsers | Where-Object { $_ })
# 盲区修正（2026-10-31 事故）：扩展太新时连不上 daemon，browsers / version_skew_browsers
# 双双为空，纯按「扩展版本 > CLI 版本」比较永远判不领先——升级死锁。故「读不到任何扩展
# 版本 + CLI 有新版」也按待追平处理：CLI 升级不需要浏览器在线。
$noExtensionVisible = -not $ext.Version
$cliHasNewer = ([string]$update.status -ne 'up_to_date')
$shouldUpgrade = $extAhead -or ($skewed.Count -gt 0) -or ($noExtensionVisible -and $cliHasNewer)
if ($shouldUpgrade) {
    if ($extAhead) {
        $why = "扩展已升级到 $($ext.Text)，CLI 仍为 $($update.current_version)"
    } elseif ($skewed.Count -gt 0) {
        $why = "存在版本错配（$($skewed -join ', ')）"
    } else {
        $why = "CLI $($update.current_version) -> $($update.latest_version) 有新版，且读不到扩展版本（可能因协议不兼容连不上）"
    }
    Write-Line "$why：执行 bsk update -y 追平"
    # 原生命令往 stderr 写东西时，$ErrorActionPreference='Stop' 会把重定向变成终止错误，
    # 故这段临时降级为 Continue，改用退出码判成败。
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $upgradeOut = ''
    $upgradeCode = -1
    try {
        $upgradeOut = (& $bsk update -y 2>&1) -join ' '
        $upgradeCode = $LASTEXITCODE
    } catch {
        $upgradeOut = $_.Exception.Message
    } finally { $ErrorActionPreference = $prevEap }
    if ($upgradeCode -ne 0) {
        $why = if ($upgradeOut) { $upgradeOut.Substring(0, [Math]::Min(200, $upgradeOut.Length)) } else { '无输出' }
        Write-Line "FAIL bsk update 退出码 $upgradeCode：$why"
    }
    # bsk update 只换二进制，**不重启由宿主持有的 daemon**（host_managed，见 2026-11-02 实测
    # "daemon: left_to_host"）。旧 daemon 与新扩展协议仍不兼容，不先 stop 等于白升：
    # 停掉后下一次抓取的 BskBrowser.ensure_daemon() 会用新二进制拉起。
    & $bsk daemon stop *> $null
    # 升级后复检：daemon 已被停掉，状态读不到属正常，这里只确认 CLI 版本已变。
    $update = Get-BskUpdateInfo $bsk
    if (-not $update -or -not $update.status) {
        Write-Line 'FAIL 追平后 bsk update --check 仍拿不到有效 JSON。'
        exit 1
    }
    $probe = Get-BskStatusInfo $bsk -Retries $STATUS_RETRIES_AFTER_UPGRADE
    $status = $probe.Data
    $statusErr = $probe.Err
    $cliVer = ConvertTo-BskVersion ([string]$update.current_version)
}

$cliCurrent = [string]$update.current_version
$cliLatest = [string]$update.latest_version
$cliStatus = [string]$update.status
$releaseUrl = [string]$update.release_url

$parts = @()
$updateAvailable = ($cliStatus -ne 'up_to_date')
$parts += "CLI $cliCurrent -> $cliLatest ($cliStatus)"
if ($updateAvailable -and $releaseUrl) { $parts += "release=$releaseUrl" }

$hasSkew = $false
$stillBehind = ($ext.Version -and $cliVer -and $ext.Version -gt $cliVer)
if ($status -and $status.daemon_version) {
    $parts += "daemon $($status.daemon_version)"
    $extVersions = @()
    foreach ($b in @($status.browsers)) {
        if ($b -and $b.extension_version) { $extVersions += "$($b.browser_name) $($b.extension_version)" }
    }
    if ($extVersions.Count -gt 0) { $parts += '扩展 ' + ($extVersions -join ', ') }
    else { $parts += '扩展 无已连接浏览器' }
    # 过 Where-Object 再数：字段缺失时 @($null).Count 等于 1，会把「没这个字段」误报成「版本错配」。
    $skewed = @($status.version_skew_browsers | Where-Object { $_ })
    if ($skewed.Count -gt 0) {
        $hasSkew = $true
        $parts += '版本错配 ' + ($skewed -join ', ')
    }
    else { $parts += '无版本错配' }
}
else {
    $reason = if ($statusErr) { ($statusErr.Substring(0, [Math]::Min(110, $statusErr.Length))) } else { '原因未知' }
    $parts += "daemon 状态未知（bsk status 失败：$reason）"
}
if ($stillBehind) { $parts += "扩展 $($ext.Text) 仍领先 CLI $cliCurrent（自动追平未生效）" }

Write-Line ($parts -join ' | ')

if ($hasSkew) {
    Write-Line '提示：扩展走浏览器扩展渠道更新，bsk CLI 无法更新扩展；请手动更新扩展。'
}
if ($updateAvailable -or $hasSkew -or $stillBehind) { exit 2 }
exit 0
