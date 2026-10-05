@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo 還沒安裝。請先雙擊 install.bat
  exit /b 1
)
set BREEZE_OPEN_BROWSER=1
REM Process environment wins. .env only fills BREEZE_PORT when it is not already set.
if not defined BREEZE_PORT if exist .env (
  for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    if /I "%%A"=="BREEZE_PORT" if not defined BREEZE_PORT set "BREEZE_PORT=%%B"
  )
)
if not defined BREEZE_PORT set BREEZE_PORT=8780
echo 主持頁請開 http://127.0.0.1:%BREEZE_PORT%
echo 聽眾才走區網。上傳與設定需要這台電腦核發的權杖。
echo 服務埠、QR 與設定使用同一個 BREEZE_PORT。
.venv\Scripts\python.exe -m app.run
if errorlevel 1 exit /b 1
