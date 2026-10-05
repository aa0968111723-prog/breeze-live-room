@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
if exist .venv\Scripts\python.exe goto dependencies
py -3.11 -m venv .venv
if errorlevel 1 py -3.12 -m venv .venv
if errorlevel 1 python -m venv .venv
if errorlevel 1 (
  echo 請先安裝 Python 3.11 或 3.12，安裝時勾選 Add Python to PATH。
  if not defined BREEZE_NONINTERACTIVE pause
  exit /b 1
)
:dependencies
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto failed
.venv\Scripts\python.exe scripts\install_runtime.py
if errorlevel 1 goto failed
if not defined BREEZE_NONINTERACTIVE pause
exit /b 0
:failed
echo 安裝未完成，請依上方原因修正後重跑。既有設定與逐字稿保留。
if not defined BREEZE_NONINTERACTIVE pause
exit /b 1
