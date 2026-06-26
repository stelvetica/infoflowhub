# InfoFlowHub environment bootstrap.
# Resolves a usable Python interpreter, (re)creates .venv from the host Python if needed,
# installs requirements on demand, and prints the final python.exe path on stdout.
# Intent: make the project machine-independent. On a fresh machine, just run the
# launcher .bat and this script wires everything up using that machine's own Python.
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir
Set-Location $root

$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$reqFile = Join-Path $root "requirements.txt"
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

# 1. Resolve target python: INFOFLOW_PYTHON > working .venv > host python > py launcher
$python = $env:INFOFLOW_PYTHON
if (-not [string]::IsNullOrWhiteSpace($python)) {
    if (-not (Test-Path $python)) { throw "INFOFLOW_PYTHON not found: $python" }
} elseif (Test-PythonOk $venvPython) {
    $python = $venvPython
} else {
    $hostPython = $null
    try { $hostPython = (Get-Command python -ErrorAction Stop).Source } catch {}
    if (-not $hostPython) {
        try { $hostPython = (& py -3.12 -c "import sys; print(sys.executable)" 2>$null).Trim() } catch {}
    }
    if (-not $hostPython) {
        try { $hostPython = (& py -3 -c "import sys; print(sys.executable)" 2>$null).Trim() } catch {}
    }
    if (-not $hostPython) { throw "Python not found. Install Python 3.12/3.13 or set INFOFLOW_PYTHON." }
    $ver = Get-PyVersion $hostPython
    if ($ver -notmatch '^3\.(12|13|14)$') { throw "Python >=3.12,<3.15 required, got $ver ($hostPython)" }

    if (-not (Test-Path $venvPython)) {
        Write-Host "[ensure] Creating .venv from host Python $hostPython ($ver) ..." -ForegroundColor Cyan
        & $hostPython -m venv "$root\.venv"
        if ($LASTEXITCODE -ne 0) { throw "Failed to create .venv with $hostPython" }
    }
    $python = $venvPython
}

# 2. Ensure core deps are importable
if (-not (Test-PythonOk $python)) {
    Write-Host "[ensure] Installing dependencies from $reqFile ..." -ForegroundColor Cyan
    & $python -m pip install --upgrade pip --quiet 2>$null
    & $python -m pip install -r $reqFile
    if ($LASTEXITCODE -ne 0) { throw "Dependency install failed. Check network / requirements.txt." }
    if (-not (Test-PythonOk $python)) { throw "Dependencies still not importable after install." }
}

Write-Output $python
