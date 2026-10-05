# breeze-live-room

桌面軟體：本機 Breeze ASR 25 聽中文，穩定句再英譯，手機平板掃 QR 看英文字幕。

專案：https://github.com/aa0968111723-prog/breeze-live-room

這不是 Qwen 那種 2.3 秒同傳。聽寫留在這台電腦，英譯才會出去（若有設 OpenAI 金鑰）。

## 這台暫時機能不能跑

2026-10-05 看過的主持機（DESKTOP-P8RGA3A，暫時用）：

- AMD Ryzen 5 5600H with Radeon Graphics，3.30 GHz
- 記憶體 16GB（可用 15.4GB），3200 MT/s
- 顯示卡是 AMD Radeon 內顯，Windows 寫 496 MB，沒有 NVIDIA
- 碟 477GB，當時已用 255GB
- Windows 11 Pro 25H2

結論：能跑 q5，不能走 CUDA。字幕大概晚 6–15 秒。換電腦也用同一套；有 6–8GB NVIDIA 或 M 系列 16GB 會更跟得上。

## 安裝

1. 安裝 Python 3.11，勾選 Add to PATH。
2. 克隆此專案。
3. 雙擊 `install.bat`。它會建虛擬環境、裝套件、下載約 1.1GB 的 q5 模型，並在桌面建「禪譯聽眾房」捷徑。
4. 從 https://github.com/ggml-org/whisper.cpp/releases 下載 Windows 的 `whisper-cli.exe`，放到 `tools\whisper-cli.exe`。
5. 安裝 ffmpeg，或把 `ffmpeg.exe` 放進 `tools\`。瀏覽器送出的是 webm，要先轉 16 kHz 單聲道 wav。
6. 英譯：設定環境變數 `OPENAI_API_KEY`。沒設也能開房，聽眾只看中文。
7. 雙擊桌面捷徑或 `start.bat`。瀏覽器開 http://127.0.0.1:8780 ，按開始聽。
8. 手機平板連同一個 Wi-Fi，掃 QR。第一次請允許 Windows 防火牆放行 8780。

QR 使用區網 IP，不是 localhost。沒有區網時聽眾連不上。

## 模型

- 名稱：MediaTek Research Breeze ASR 25
- 來源：https://huggingface.co/MediaTek-Research/Breeze-ASR-25
- 架構：Whisper-large-v2 微調，約 15.5 億參數，Apache 2.0
- 擅長：台灣國語、中英夾雜。只聽寫，不翻譯。
- 本專案下載：https://huggingface.co/shdennlin/breeze-asr-25-ggml 的 `ggml-breeze-asr-25-q5_0.bin`（約 1.1GB）
- q8 約 1.7GB，品質更接近 fp16；這顆 CPU 建議先 q5。
- 權重不進 Git，不進 Cloudflare / Zeabur 映像。

熱詞：Breeze 沒有 Qwen 那種鎖定。程式把前面這段放進 `initial_prompt`：

`以下是普通話的句子，請用繁體中文輸出。常見專有名詞：般若、菩提心、空性、因緣。`

這是偏置，不保證鎖詞。譯文用詞要放翻譯層。

## 聽眾房

- 主持頁：`/`
- 聽眾頁：`/r/{id}`，預設 `/r/class`
- 聽眾 WebSocket：`/ws/listen?room_id=`
- QR：`/api/qr?room_id=`，網址是區網 IP + 埠 8780
- 只推已辨識完的句子。斷線重連會補最近歷史。
- 免安裝、免帳號。手機平板用瀏覽器開。

## 現在沒做到的

- 不是單一 exe / MSI，是 `install.bat` 加桌面捷徑。
- 沒有 Qwen realtime 一條長連線。
- 沒有設定頁切換聽寫引擎。
- 房間在記憶體，重開就沒了。
- 標點要後處理，Breeze 訓練時拿掉了標點。

完整說明見 `docs/SPEC.md`。
