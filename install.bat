@echo off
setlocal
cd /d %~dp0
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
  echo 下載 Breeze ASR 25 q5 模型，約 1.1GB...
  powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://huggingface.co/shdennlin/breeze-asr-25-ggml/resolve/main/ggml-breeze-asr-25-q5_0.bin' -OutFile 'models\\ggml-breeze-asr-25-q5_0.bin.part'"
  if errorlevel 1 (
    echo 模型下載失敗。安裝未完成。
    exit /b 1
  )
  move /y models\ggml-breeze-asr-25-q5_0.bin.part models\ggml-breeze-asr-25-q5_0.bin
  if errorlevel 1 exit /b 1
)
echo.
echo 安裝步驟已跑完。
echo 請再放入 tools\whisper-cli.exe 與 ffmpeg。權重不進 Git。
echo 英譯可選：複製 .env.example 為 .env 後再填 OPENAI_API_KEY。沒有金鑰也能只出中文。
echo 然後雙擊 start.bat。這支安裝程式沒有在這台 Linux 查核機上執行過。
exit /b 0
