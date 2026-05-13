$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$port = 18421
$bindHost = "127.0.0.1"

$python = $env:INFOFLOW_PYTHON
if ([string]::IsNullOrWhiteSpace($python)) {
  $python = "python"
}

$logDir = Join-Path $root "runtime"
$logPath = Join-Path $logDir "web.log"

if (!(Test-Path $logDir)) {
  New-Item -ItemType Directory -Path $logDir | Out-Null
}

function Stop-InfoFlowPortProcess {
  param(
    [int]$TargetPort
  )

  $lines = netstat -ano -p tcp | Select-String ":$TargetPort"
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
    try {
      Stop-Process -Id $_ -Force -ErrorAction Stop
    } catch {
    }
  }
}

function Wait-PortReleased {
  param(
    [int]$TargetPort,
    [int]$TimeoutSeconds = 15
  )

  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  do {
    $stillListening = netstat -ano -p tcp | Select-String "LISTENING\s+\d+$" | Select-String ":$TargetPort"
    if (-not $stillListening) {
      return
    }
    Start-Sleep -Milliseconds 500
  } while ((Get-Date) -lt $deadline)

  throw "Port $TargetPort was not released in time."
}

Stop-InfoFlowPortProcess -TargetPort $port

Get-CimInstance Win32_Process -Filter "name = 'python.exe' or name = 'pythonw.exe'" |
  Where-Object { $_.CommandLine -like "*$root*" -and $_.CommandLine -like "*uvicorn*" } |
  ForEach-Object {
    try {
      Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
    } catch {
    }
  }

Wait-PortReleased -TargetPort $port

$command = "Set-Location '$root'; & '$python' -m uvicorn web.app:app --host $bindHost --port $port *>> '$logPath'"
Start-Process powershell -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-Command", $command -WindowStyle Hidden
