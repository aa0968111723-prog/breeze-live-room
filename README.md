# breeze-live-room

桌面軟體：本機 Breeze ASR 25 聽中文，穩定句再英譯，手機平板掃 QR 看英文字幕。

這台暫時的 Ryzen 5 5600H 沒有 NVIDIA。辨識走 whisper.cpp CPU，q5 模型約 1.1GB。字幕會晚數秒到十幾秒，不是 2 秒同傳。

## 安裝

1. 安裝 Python 3.11（勾選 Add to PATH）。
2. 雙擊 `install.bat`。它會建虛擬環境、裝套件、下載 q5 模型，並在桌面建捷徑。
3. 從 https://github.com/ggml-org/whisper.cpp/releases 下載 Windows 的 `whisper-cli.exe`，放到 `tools\whisper-cli.exe`。
4. 英譯要設環境變數 `OPENAI_API_KEY`。沒設也能開房，聽眾只看中文。
5. 雙擊桌面的「禪譯聽眾房」或 `start.bat`。

手機要跟電腦同一個 Wi-Fi。第一次請允許 Windows 防火牆放行 8780。

模型權重不進 Git。
