@echo off
setlocal
cd /d "%~dp0"
py -3.11 -m venv .venv
if errorlevel 1 py -3 -m venv .venv
if errorlevel 1 (
  echo 找不到 Python 3.11。安裝未完成。
  exit /b 1
)
call .venv\Scripts\activate.bat
if errorlevel 1 (
  echo 虛擬環境沒有啟動。安裝未完成。
  exit /b 1
)
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo 套件安裝失敗。安裝未完成。
  exit /b 1
)
if not exist models mkdir models
if not exist tools mkdir tools
if not exist tmp mkdir tmp
if not exist models\ggml-breeze-asr-25-q5_0.bin (
  powershell -NoProfile -Command "$free=(Get-PSDrive -Name ($pwd.Drive.Name)).Free; if ($free -lt 5GB) { exit 2 }"
  if errorlevel 2 (
    echo 磁碟剩餘空間不足 5GB。安裝未完成。
    exit /b 1
  )
  echo 下載 Breeze ASR 25 q5 模型，約 1.1GB...
  curl.exe -L --fail --retry 3 --retry-all-errors -C - -o "models\ggml-breeze-asr-25-q5_0.bin.part" "https://huggingface.co/shdennlin/breeze-asr-25-ggml/resolve/main/ggml-breeze-asr-25-q5_0.bin"
  if errorlevel 1 (
    echo 模型下載失敗。安裝未完成。
    exit /b 1
  )
  powershell -NoProfile -Command "$p='models\ggml-breeze-asr-25-q5_0.bin.part'; $len=(Get-Item $p).Length; $head=Get-Content -Encoding Byte -TotalCount 1 $p; if ($len -lt 500MB -or $head[0] -eq 60) { exit 3 }"
  if errorlevel 3 (
    echo 下載內容不像模型檔。安裝未完成。
    exit /b 1
  )
  if exist models\ggml-breeze-asr-25-q5_0.bin.sha256 (
    powershell -NoProfile -Command "$expected=(Get-Content 'models\ggml-breeze-asr-25-q5_0.bin.sha256' -Raw).Split()[0].ToLower(); $actual=(Get-FileHash 'models\ggml-breeze-asr-25-q5_0.bin.part' -Algorithm SHA256).Hash.ToLower(); if ($actual -ne $expected) { exit 4 }"
    if errorlevel 4 (
      echo 模型雜湊不符。安裝未完成。
      exit /b 1
    )
  )
  move /y models\ggml-breeze-asr-25-q5_0.bin.part models\ggml-breeze-asr-25-q5_0.bin
  if errorlevel 1 exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-shortcut.ps1" -WorkingDirectory "%~dp0."
if errorlevel 1 (
  echo 桌面捷徑沒有建立。其餘安裝步驟若上面沒有報錯，程式仍可用 start.bat 啟動。
)
REM 無法查核 whisper.cpp 發行版雜湊，腳本不內嵌 sha256。Windows zip 必須由操作者自行提供並檢查。
echo.
echo 安裝步驟已跑完。
echo 請操作者自行提供並檢查 whisper.cpp Windows zip 後，再放入 tools\whisper-cli.exe、whisper-server.exe，以及 ffmpeg 與其 DLL。本腳本沒有核對該發行雜湊。
echo 權重不進 Git。若有 sha256，放到 models\ggml-breeze-asr-25-q5_0.bin.sha256 後重跑可檢查。
echo 英譯可選：複製 .env.example 為 .env 後再填 OPENAI_API_KEY。沒有金鑰也能只出中文。
echo 然後雙擊桌面捷徑或 start.bat。這支安裝程式沒有在這台 Linux 查核機上執行過。
exit /b 0
