@echo off
cd /d %~dp0
if not exist .venv\Scripts\python.exe (
  echo 還沒安裝。請先雙擊 install.bat
  exit /b 1
)
echo 主持頁請開 http://127.0.0.1:8780
echo 聽眾才走區網。上傳與設定需要這台電腦核發的權杖。
.venv\Scripts\python.exe -m uvicorn app.server:app --host 0.0.0.0 --port 8780
if errorlevel 1 exit /b 1
