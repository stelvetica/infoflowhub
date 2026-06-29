# InfoFlowHub environment bootstrap (offline-capable, machine-independent).
#
# Resolution order for the python interpreter:
#   1. INFOFLOW_PYTHON env var (explicit override)
#   2. A working project .venv (reuse if it imports core deps)
#   3. Bundled portable Python (runtime/python-portable) + offline wheels (runtime/wheels)
#      -> fully offline, no network. This is the "copy folder and run" path on a bare machine.
#   4. Host python + online pip fallback (when neither .venv nor portable python are bundled)
#
# Prints the final python.exe path on stdout.
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir
Set-Location $root

$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$reqFile = Join-Path $root "requirements.txt"
$portablePython = Join-Path $root "runtime\python-portable\python.exe"
$offlineWheels = Join-Path $root "runtime\wheels"
$coreProbe = "import fastapi,uvicorn,jinja2,feedparser,httpx,requests,dotenv,yaml"

function Test-PythonOk($exe) {
    if (-not $exe -or -not (Test-Path $exe)) { return $false }
    try {
        $null = & $exe -c $coreProbe 2>$null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

function Get-PyVersion($exe) {
    try { return (& $exe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null).Trim() }
    catch { return $null }
}

function New-VenvFrom($basePython) {
    & $basePython -m venv "$root\.venv"
    if ($LASTEXITCODE -ne 0) { throw "Failed to create .venv with $basePython" }
}

# 1. Explicit override or existing working .venv
$python = $env:INFOFLOW_PYTHON
if (-not [string]::IsNullOrWhiteSpace($python)) {
    if (-not (Test-Path $python)) { throw "INFOFLOW_PYTHON not found: $python" }
} elseif (Test-PythonOk $venvPython) {
    $python = $venvPython
} else {
    # 2. Bundled portable python + offline wheels (no network needed)
    if (Test-Path $portablePython) {
        if (-not (Test-Path $venvPython)) {
            Write-Host "[ensure] Creating .venv from bundled portable Python ..." -ForegroundColor Cyan
            New-VenvFrom $portablePython
        }
        if (-not (Test-PythonOk $venvPython)) {
            Write-Host "[ensure] Installing dependencies from offline wheels ..." -ForegroundColor Cyan
            & $venvPython -m pip install --no-index --find-links "$offlineWheels" -r $reqFile
            if ($LASTEXITCODE -ne 0) { throw "Offline dependency install failed." }
        }
        $python = $venvPython
    } else {
        # 3. Host python + online pip fallback
        $hostPython = $null
        try { $hostPython = (Get-Command python -ErrorAction Stop).Source } catch {}
        if (-not $hostPython) { try { $hostPython = (& py -3.12 -c "import sys; print(sys.executable)" 2>$null).Trim() } catch {} }
        if (-not $hostPython) { try { $hostPython = (& py -3 -c "import sys; print(sys.executable)" 2>$null).Trim() } catch {} }
        if (-not $hostPython) { throw "No Python found. Bundle runtime/python-portable, install Python 3.12/3.13, or set INFOFLOW_PYTHON." }
        $ver = Get-PyVersion $hostPython
        if ($ver -notmatch '^3\.(12|13|14)$') { throw "Python >=3.12,<3.15 required, got $ver ($hostPython)" }

        if (-not (Test-Path $venvPython)) {
            Write-Host "[ensure] Creating .venv from host Python $hostPython ($ver) ..." -ForegroundColor Cyan
            New-VenvFrom $hostPython
        }
        if (-not (Test-PythonOk $venvPython)) {
            Write-Host "[ensure] Installing dependencies online ..." -ForegroundColor Cyan
            & $venvPython -m pip install --upgrade pip --quiet 2>$null
            & $venvPython -m pip install -r $reqFile
            if ($LASTEXITCODE -ne 0) { throw "Online dependency install failed. Check network / requirements.txt." }
        }
        $python = $venvPython
    }
}

if (-not (Test-PythonOk $python)) { throw "Resolved python is not usable: $python" }
Write-Output $python
