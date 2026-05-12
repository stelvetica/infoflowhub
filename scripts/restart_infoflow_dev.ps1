$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir

Set-Location $root

& (Join-Path $scriptDir "start_infoflow.ps1")

$url = "http://127.0.0.1:18421/"
$deadline = (Get-Date).AddSeconds(45)
do {
  Start-Sleep -Seconds 1
  try {
    $resp = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3
    if ($resp.StatusCode -eq 200 -and $resp.Content -match "InfoFlowHub") {
      Start-Process $url | Out-Null
      exit 0
    }
  } catch {
  }
} while ((Get-Date) -lt $deadline)

throw "InfoFlowHub 未在 45 秒内启动完成：$url"
