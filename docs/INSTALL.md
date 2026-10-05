# 安裝

日期：2026-10-05。這份說明對齊程式提交 `cb21186d35a50690b7f012f35b4423b88b7ff6d4`。

這台是 Linux 查核機，不是 Windows 主持機。`install.bat`、`start.bat`、`install-shortcut.ps1` 都沒有在這裡執行。不要只複製單一 exe 就假設能跑；這次沒有在 Windows 上驗證 DLL、防火牆、埠佔用，或含中文與空白的路徑。

## 環境變數

`.env` 由 `fill_process_environ` 與 `Settings.from_env` 載入。只填行程裡還沒有的鍵，已存在的行程環境贏過檔案，值不會被印出來。

`start.bat` 與 `python -m app.run` 共用 `BREEZE_PORT`。批次檔只在變數尚未設定時才從 `.env` 讀埠，否則用 8780，再把該值留在行程環境。`app.run` 不會用檔案蓋掉它。主持頁、服務與 QR 都用這個埠。

## 步驟

1. 安裝 Python 3.11，勾選 Add to PATH。
2. 克隆專案，看分支 `fix/round2-continue` 的提交 `cb21186d35a50690b7f012f35b4423b88b7ff6d4`。
3. 雙擊 `install.bat`。失敗時不會說安裝完成。磁碟剩餘少於 5GB 會中止，不下載。
4. 模型網址仍是 https://huggingface.co/shdennlin/breeze-asr-25-ggml/resolve/main/ggml-breeze-asr-25-q5_0.bin （約 1.1GB 的 q5）。雜湊沒有在這台計算，所以沒有釘選。只有你另外放了 `models\ggml-breeze-asr-25-q5_0.bin.sha256` 時，`install.bat` 才會比對。權重不進 Git。
5. 放入固定版本的 whisper.cpp Windows 套件：`tools\whisper-cli.exe`、要用常駐時再加 `tools\whisper-server.exe`，以及 ffmpeg 與其 DLL。
6. 英譯可選。複製 `.env.example` 為 `.env`，再填 `OPENAI_API_KEY`。沒有金鑰是正常的中文模式。不要提交 `.env`。
7. 桌面捷徑由 `install-shortcut.ps1` 建立，名稱是「禪譯聽眾房」，目標是 `start.bat`。腳本存在，這裡沒執行。捷徑失敗時仍可用 `start.bat`。
8. 雙擊 `start.bat`。主持頁是 `http://127.0.0.1:<BREEZE_PORT>`。手機要和電腦同一個 Wi-Fi，掃畫面上的 QR。沒有可分享位址時不會出 QR，也不會改成 localhost。

第一次請自行允許 Windows 防火牆放行該埠。這件事沒有在這裡驗證。
