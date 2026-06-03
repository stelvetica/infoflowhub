@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
set "POWERSHELL_EXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"

if not exist "%POWERSHELL_EXE%" set "POWERSHELL_EXE=powershell"

pushd "%SCRIPT_DIR%" >nul
"%POWERSHELL_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%start_infoflow_web.ps1"
set "EXIT_CODE=%ERRORLEVEL%"
popd >nul

endlocal & exit /b %EXIT_CODE%
