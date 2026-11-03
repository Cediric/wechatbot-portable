@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
if not exist "%~dp0portable\runtime\bridge-venv\Scripts\python.exe" (
  echo 本包桥接环境尚未就绪，请先运行“检测修复环境.bat”。
  pause
  exit /b 1
)
"%~dp0portable\runtime\bridge-venv\Scripts\python.exe" -X utf8 -B "%~dp0portable\collect_diagnostics.py"
pause
endlocal
