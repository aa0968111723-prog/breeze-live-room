# 完整規格

## 目標

長時間持續字幕。聽寫預設本機 Breeze ASR 25。英譯可關。聽眾房必做。聽寫、翻譯、分享分開，不要寫死。

## 四個旋鈕

1. 聽寫：breeze | qwen-live | openai。現在專案只實作 breeze。
2. 送法：chunk。這台 CPU 用約 6 秒一段，不要假裝成 Qwen 串流。
3. 翻譯：OpenAI（`OPENAI_API_KEY`，預設 gpt-4.1-mini）。沒金鑰就只出中文。
4. 分享：room。預設開。

## 資料流

1. 瀏覽器麥克風每 6 秒送一段。
2. ffmpeg 轉 16 kHz 單聲道 wav。
3. whisper-cli 載 Breeze q5，語言 zh，帶 initial_prompt。
4. 有金鑰才英譯。問題只翻譯，不代答。
5. 已落定的中英句子廣播給 `/ws/listen`。
6. 聽眾頁大字顯示英文，下方留中文。

## 配備參考

- q5 檔案約 1.1GB，q8 約 1.7GB，fp16 約 2.9GB。
- faster-whisper large-v2：fp16 約 4.5GB 顯存，int8 約 2.9GB。這台沒獨顯，不走這條。
- CPU：16GB RAM 夠載 q5/q8。磁碟留 5GB。
- 想字幕更跟：NVIDIA 6–8GB，或 Apple Silicon 16GB。

## 禁止

- 不把權重打進 Git、Worker 或映像。
- 不讓 Breeze 出英譯。
- 不生成 localhost QR。
- 不把金鑰寫進前端或儲存庫。

## 相關專案

- zen-bridge：禪譯工作台，目前沒有聽眾房。
- tku-live-translate：已有 QR 房間形狀，這個專案是桌面可安裝版。
