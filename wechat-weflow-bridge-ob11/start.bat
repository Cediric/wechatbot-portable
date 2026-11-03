@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "AKASHA_DIR=%~dp0"
cd /d "%AKASHA_DIR%"
if exist "%AKASHA_DIR%\.venv\Scripts\python.exe" (
  "%AKASHA_DIR%\.venv\Scripts\python.exe" "%AKASHA_DIR%startup_launcher.py" bridge --foreground
  goto done
)
python --version >nul 2>nul
if not errorlevel 1 (
  python "%AKASHA_DIR%startup_launcher.py" bridge --foreground
  goto done
)
py -3 "%AKASHA_DIR%startup_launcher.py" bridge --foreground
:done
if errorlevel 1 (
  echo 启动未完成，请查看上方提示和本项目 startup 日志。
  pause
)
endlocal
