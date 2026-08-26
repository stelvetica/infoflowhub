@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
set "POWERSHELL_EXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"

if not exist "%POWERSHELL_EXE%" set "POWERSHELL_EXE=powershell"

echo ============================================
echo   InfoFlowHub service is starting...
echo ============================================
echo.

pushd "%SCRIPT_DIR%" >nul
"%POWERSHELL_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%start_infoflow_web.ps1"
set "EXIT_CODE=%ERRORLEVEL%"
popd >nul

echo.
echo ============================================
if "%EXIT_CODE%"=="0" goto :ok
echo   [FAIL] Start failed. Exit code %EXIT_CODE%
echo   Please check the log above, or confirm port 18421 is free.
goto :done
:ok
echo   [OK] Service started:  http://127.0.0.1:18421/
:done
echo ============================================
echo.
pause
endlocal & exit /b %EXIT_CODE%
