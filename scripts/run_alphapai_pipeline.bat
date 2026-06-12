@echo off
setlocal enabledelayedexpansion
title Alpha派蓝宝书 每日抓取

echo [%date% %time%] === Alpha派蓝宝书抓取开始 ===

rem === 优雅关闭 Chrome（不带 /f） ===
echo 关闭日常 Chrome...
taskkill /im chrome.exe /t 2>nul
timeout /t 3 /nobreak >nul

rem === 检查 Chrome 是否完全关闭 ===
tasklist /fi "imagename eq chrome.exe" 2>nul | find /i "chrome.exe" >nul
if %errorlevel% equ 0 (
    echo [警告] Chrome 未完全关闭，尝试再次关闭...
    taskkill /im chrome.exe /t 2>nul
    timeout /t 2 /nobreak >nul
    tasklist /fi "imagename eq chrome.exe" 2>nul | find /i "chrome.exe" >nul
    if %errorlevel% equ 0 (
        echo [失败] Chrome 无法关闭，跳过本次抓取
        exit /b 1
    )
)

echo [%date% %time%] Chrome 已关闭，开始抓取蓝宝书...

rem === 执行抓取 ===
cd /d C:\Users\TB14Plus\Playground\infoflowhub
C:\Users\TB14Plus\.workbuddy\binaries\python\envs\default\Scripts\python.exe -m apps.subscriptions.rss_pipeline fetch --source alphapai

set EXIT_CODE=%errorlevel%
echo [%date% %time%] 抓取完成 (exit=%EXIT_CODE%)

exit /b %EXIT_CODE%
