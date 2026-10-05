@echo off
cd /d %~dp0
py -3.11 -m venv .venv 2>nul || py -3 -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt
if not exist models mkdir models
if not exist tools mkdir tools
if not exist models\ggml-breeze-asr-25-q5_0.bin (
  echo 下載 Breeze ASR 25 q5 模型（約 1.1GB）...
  powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://huggingface.co/shdennlin/breeze-asr-25-ggml/resolve/main/ggml-breeze-asr-25-q5_0.bin' -OutFile 'models\ggml-breeze-asr-25-q5_0.bin'"
)
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\禪譯聽眾房.lnk'); $s.TargetPath='%~dp0start.bat'; $s.WorkingDirectory='%~dp0'; $s.Save()"
echo 安裝完成。請把 whisper-cli.exe 放進 tools\
