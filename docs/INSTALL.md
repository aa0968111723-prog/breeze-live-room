# 安裝

這份說明對齊目前程式。Windows 安裝腳本沒有在查核機上執行過。

1. 安裝 Python 3.11，勾選 Add to PATH。
2. 克隆專案。
3. 雙擊 `install.bat`。失敗時不會說安裝完成。
4. 放入 `tools\whisper-cli.exe` 與 ffmpeg。不要只複製單一 exe 就假設能跑，這次沒有在 Windows 上驗證 DLL。
5. 英譯可選。複製 `.env.example` 為 `.env`，填 `OPENAI_API_KEY`。沒有金鑰是正常的中文模式。
6. 雙擊 `start.bat`。主持頁開 `http://127.0.0.1:8780`。手機要和電腦同一個 Wi-Fi，掃畫面上的 QR。沒有區網位址時不會出 QR。

權重不下載進 Git。
