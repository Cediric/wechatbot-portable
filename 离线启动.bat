@echo off
chcp 65001 >nul
set WECHATBOT_OFFLINE=1
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0portable\bootstrap.ps1" -Action all
if errorlevel 1 pause
