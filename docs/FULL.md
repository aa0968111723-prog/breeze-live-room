# 禪譯聽眾房完整資訊

專案：https://github.com/aa0968111723-prog/breeze-live-room

桌面主持端。電腦用聯發科 Breeze ASR 25 聽中文，穩定句再譯成英文，手機和平板掃 QR 看英文字幕。模型權重不進 Git。

## 這台暫時機

- 名稱：DESKTOP-P8RGA3A
- 系統：Windows 11 Pro 25H2
- 處理器：AMD Ryzen 5 5600H with Radeon Graphics，3.30 GHz，6 核 12 執行緒
- 記憶體：16 GB（15.4 GB 可用）
- 顯示：AMD Radeon 內顯，關於畫面顯示 496 MB，沒有 NVIDIA 獨顯
- 磁碟：477 GB，已用約 255 GB

這台可以跑，但只能 CPU。不是正式機也能用同一個專案重裝。

## 模型

- 名稱：MediaTek Research Breeze ASR 25
- 來源：https://huggingface.co/MediaTek-Research/Breeze-ASR-25
- 基底：Whisper-large-v2，約 15.5 億參數
- 授權：Apache 2.0
- 擅長：台灣國語、台灣口音、中英夾雜
- 沒有 Qwen 那種熱詞鎖定。只能把前 30 個詞放進 initial_prompt
- 標點在訓練時多半被拿掉，字幕標點要後處理
- 本專案用 whisper.cpp GGML q5，檔案約 1.1GB：https://huggingface.co/shdennlin/breeze-asr-25-ggml
- q8 約 1.7GB，品質更接近完整版；這台 16GB 建議先用 q5

## 配備需求

- 最低：8GB RAM 可跑 q5，但長堂課不安
- 建議：16GB RAM。有 NVIDIA 6–8GB 顯存會比這台快
- Apple M 系列可用 MLX，這個 Windows 專案不直接支援
- 磁碟留 5GB 給權重與暫存
- 不需要天璞晶片或 NPU

## 這個軟體做什麼

1. 主持頁用瀏覽器麥克風聽中文。
2. 約每 6 秒一段送給本機 whisper.cpp + Breeze。
3. 辨識出來的繁中穩定句，若設了 OPENAI_API_KEY，再譯成英文。問題只翻譯，不代答。
4. 譯文落定後推到聽眾房 /r/class。
5. 手機平板開連結或掃 QR，免安裝，看英文字幕。斷線重連會補最近字幕。
6. 音檔留在這台電腦，不送聯發科。

## 這個軟體不做什麼

- 不是 Qwen3.8-LiveTranslate 那種平均 2.3 秒同傳。
- 這台 CPU 字幕大概晚 6–15 秒，句子長或電腦忙時更慢。
- 不克隆聲音，不播合成語音，避免回授。
- 沒有把權重打進安裝檔。模型第一次由 install.bat 下載。
- 不是已封裝的單一 exe。現在是桌面捷徑加本機伺服器。
- 聽眾要和電腦同一個 Wi-Fi。QR 不能是別人連不到的 localhost。

## 安裝

1. 安裝 Python 3.11，勾選 Add to PATH。
2. 克隆此專案。
3. 雙擊 install.bat。建虛擬環境、裝套件、下載 q5 模型、桌面捷徑「禪譯聽眾房」。
4. 從 https://github.com/ggml-org/whisper.cpp/releases 下載 Windows 的 whisper-cli.exe，放到 tools\whisper-cli.exe。
5. 英譯：設環境變數 OPENAI_API_KEY。沒設也能開房，聽眾只看中文。
6. 雙擊桌面捷徑或 start.bat。瀏覽器開主持頁，按開始聽。
7. 手機平板連同一個 Wi-Fi，掃 QR。Windows 防火牆放行 8780。

## 熱詞與翻譯

- Breeze 只負責聽寫。
- 譯文走 OpenAI。沒金鑰就只顯示中文。
- 要鎖佛教術語，放在翻譯提示，不要期望 Breeze 一定辨成那個詞。
- 詞表示例：以下是普通話的句子，請用繁體中文輸出。常見專有名詞：般若、菩提心。

## 和禪譯 zen-bridge 的關係

- zen-bridge 是淡江禪學社工作台，目前聽寫走 Qwen 或 OpenAI，沒有聽眾房。
- 這個專案專門做：本機 Breeze 聽中文 + 英文字幕房。
- tku-live-translate 已有 QR 房形狀，這裡是可安裝的桌面版，不在執行期依賴它。

## 完成標準

- 主持頁能開始聽。
- 同一 Wi-Fi 的手機開 /r/class 看到英文，沒金鑰時看到中文。
- 模型檔不在 Git 歷史裡。
- 沒有 whisper-cli.exe 或模型時，畫面要明示，不要偷偷改走雲端辨識。
