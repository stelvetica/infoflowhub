$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$npm = (Get-Command npm.cmd -ErrorAction Stop).Source
$logDir = Join-Path $root "runtime"
$logPath = Join-Path $logDir "next-dev.log"

if (!(Test-Path $logDir)) {
  New-Item -ItemType Directory -Path $logDir | Out-Null
}

Get-CimInstance Win32_Process -Filter "name = 'node.exe'" |
  Where-Object { $_.CommandLine -like "*$root*" } |
  ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force
  }

Start-Sleep -Seconds 1

$nextDir = Join-Path $root ".next"
if (Test-Path $nextDir) {
  Remove-Item -LiteralPath $nextDir -Recurse -Force
}

$command = "Set-Location '$root'; & '$npm' run dev *>> '$logPath'"
Start-Process powershell -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-Command", $command -WindowStyle Hidden
