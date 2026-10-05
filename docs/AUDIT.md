# AUDIT

基準：main 76a510bfae442d0e753d612e402d3350a09ed7d1（2026-10-05）。修正在分支 field-ready。

## 已重現 / 靜態確認

- P0 區網可未授權上傳：start.bat 綁 0.0.0.0，/api/push 無身分。位置 app/server.py。影響：同網可污染字幕並觸發英譯費用。
- P0 英譯例外會讓整段失敗：舊版 translate 例外不進 history。
- P0 每段重新呼叫 whisper-cli，模型不常駐。
- P0 host.html 可重複開始，舊計時器仍活。
- P1 聽眾斷線只叫重整理。
- 實機 30 分鐘與 2 小時：未在 DESKTOP-P8RGA3A 驗證，不得宣稱延遲已量測。

## field-ready 修法

- 主持端本機取得 host token，上傳必帶 X-Host-Token。聽眾 QR 不含 token。
- 同 session/seq 去重。英譯失敗仍廣播中文。
- 單 worker 依序處理。
- 瀏覽器改送 16kHz WAV，開始按鈕鎖住。
