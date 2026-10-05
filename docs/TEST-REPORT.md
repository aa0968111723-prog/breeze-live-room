# 測試紀錄

日期：2026-10-05。機器：Linux 查核環境，不是 Ryzen 5 5600H / 16GB / Windows 11 主持機。沒有麥克風、Breeze 權重、`whisper-cli.exe`、OpenAI 金鑰。

分支 `fix/round2-continue`。程式提交是 `cb21186d35a50690b7f012f35b4423b88b7ff6d4`，接在 `5c7784ca54d9c103529d85fbd1e2a111cc89c8bc` 之上。這份 SHA 紀錄是隨後的文件提交，不是 GitHub Actions 的結果。

## 已修

這些行為在提交 `cb21186`。本地自動測試有覆蓋對應失敗路徑。這不是實機通過，GitHub Actions 也還沒跑這個提交：

- 主持權杖只在 loopback，且 Host／Origin 要對上確切埠；權杖不進 setup／QR
- `/api/push` 在解析表單前准入；同一段 single-flight；重送相同內容不佔第二個名額
- 中文先廣播，英譯失敗保留同一段中文
- RoomBus 依 `room_id` 分開；缺段與過短的補償窗口會標 missing 或 gap
- 匿名 WebSocket 不開房
- `ResidentAsr` 的 ready 旗標不是推論
- `.env` 不覆蓋行程環境；`start.bat` 與 `app.run` 共用 `BREEZE_PORT`
- 房間 id 不截斷；過大音訊拒絕；whisper-cli 非零退出不當成功
- 錄音狀態機拒絕重入；中間切片不上傳；沒有可分享位址不產生 localhost QR

## 仍未完成

- GitHub Actions 還沒跑 `cb21186`。推送前遠端 PR head 仍是 `5c7784c`。
- 模型雜湊未在此計算、未釘選。
- CLI 仍是每段重開行程。常駐「只載入一次」沒有實機證據。
- 捷徑腳本、Windows DLL、防火牆、埠、中文或空白路徑都沒在這裡跑。
- 沒有真金鑰，不能宣稱 401／429／計費已驗證。`translate_verified` 恆為 false。
- 沒有 RTF、p50、p95、主持機 RAM／CPU，也沒有「模型已在實機只載入一次」。6–15 秒不是實測。

## 自動測試已通過

2026-10-05，這台 Linux，CPython 3.11.17（`/root/breeze-live-room/.venv`），工作目錄 `/root/breeze-live-room`。沒有下載模型，沒有使用付費金鑰。rebase 之後的程式提交 `cb21186d35a50690b7f012f35b4423b88b7ff6d4` 上親見下列結果，不是 GitHub Actions 結果。

指令與結果：

- rebase 後的權威結果：`cd /root/breeze-live-room && .venv/bin/python -m pytest -q --tb=line` 印出 `34 passed in 14.78s`（exit 0）。同一輪 `node tests/recorder_machine.test.mjs` 印出 `recorder machine ok`，`node tests/room_client.test.mjs` 印出 `room client ok`（Node v26.3.1）。
- 同一批測試較早也通過：`34 passed in 11.49s`（rebase 前）、`32 passed`、`31 passed in 10.88s`、`31 passed in 10.24s`、`31 passed in 9.74s`。
- `node tests/recorder_machine.test.mjs` 印出 ok（`recorder machine ok`）；`node tests/room_client.test.mjs` 印出 ok（`room client ok`）。Node v26.3.1。CI 指定 Node 22，尚未在這個工作樹上跑。

socket `__aexit__` 與顯示順序（display-order）測試修正之前的乾淨複製是 `25 passed, 6 failed`，失敗都在 `tests/test_round2.py`，不是 `ModuleNotFoundError: No module named 'app'`。那次不能當成目前結果。GitHub Actions 還沒在這個工作樹上跑過，所以「目前 CI 通過」尚未成立。Copilot review 成功也不等於測試通過。

## 實機驗證沒有通過

30 分鐘講話、2 小時連續跑、3 台手機，以及 Ryzen 5 5600H / 16GB / Windows 11 主持機上的檢查都沒有通過。具體阻塞：這台環境跑不了該測試。不要編 RTF 或延遲。

### 30 分鐘講話（阻塞）

需要主持機、真實麥克風、`tools\whisper-cli.exe` 或已就緒的本機 whisper-server、`models\ggml-breeze-asr-25-q5_0.bin`、ffmpeg 與 DLL。

1. 跑 `install.bat`、`start.bat`，打開 `http://127.0.0.1:<BREEZE_PORT>`。
2. 確認畫面不是「還缺 whisper／模型／ffmpeg」，且沒有改走雲端。
3. 連續講話 30 分鐘。
4. 記錄崩潰、缺段、英譯失敗時中文是否仍在、停止後軌道是否釋放。
5. 匯出 SRT，對一下 t0／t1。不要填沒量到的 RTF、p50、p95、RAM、CPU，也不要寫模型只載入一次。

這台做不到，所以沒有結果。

### 2 小時連續跑（阻塞）

1. 同一主持機連續跑 2 小時，會話保持 active。
2. 確認房間不被閒置掃描清掉，`tmp` 不殘留音檔目錄。
3. 沒有金鑰就只記中文模式，不要編 401、429 或金額。
4. 不要補延遲或記憶體曲線。

這台做不到，所以沒有結果。

### 3 台手機或平板（阻塞）

1. 三台與電腦同一 Wi-Fi，掃 QR，網址不得是 localhost。
2. 三台看到同一房間。沒金鑰只要求中文。
3. 一台斷線再連，依 cursor 補段；若 gap，畫面應說明補償窗口不足。
4. 連一個未開的房間，確認沒有新房間被匿名建立。
5. 從手機呼叫主持權杖與 `/api/push`，應被拒絕。

這台沒有手機，所以沒有結果。

## 阻塞條件

- 需要 Windows 主持機、麥克風、Breeze q5 權重、whisper.cpp Windows 套件、ffmpeg 與 DLL
- 英譯 401／429／計費需要使用者自己的金鑰，這次沒有代填
