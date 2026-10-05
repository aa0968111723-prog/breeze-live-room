# breeze-live-room

桌面軟體：本機 Breeze ASR 25 聽中文，穩定句再英譯，手機平板掃 QR 看字幕。

專案：https://github.com/aa0968111723-prog/breeze-live-room

這不是 Qwen 那種 2.3 秒同傳。聽寫留在這台電腦，英譯才會出去（若有設 OpenAI 金鑰）。沒有金鑰就只出中文，不會改走雲端辨識。

## 版本狀態

- 分支：`fix/round2-continue`
- 程式提交：`cb21186d35a50690b7f012f35b4423b88b7ff6d4`
- 已快轉推上 `fix/field-ready-core`，tip 是 `4f3d947`。程式本體在 `cb21186`。工作流程檔沒有一起推上：這組權杖不能改 `.github/workflows/test.yml`。CI 因此還不會跑 `tests/room_client.test.mjs`。
- 本文件寫於 2026-10-05。這台是 Linux 查核機，直譯器是倉庫 venv 的 CPython 3.11.17（`/root/breeze-live-room/.venv`），不是 Ryzen 5 5600H / 16GB / Windows 11 主持機。這裡沒有麥克風、Breeze 權重、whisper-cli、`whisper-cli.exe`、OpenAI 金鑰。

## 已修

以提交 `cb21186` 的程式為準。尚未合併，也不能當成 GitHub Actions 已通過：

- 主持權杖只發給 loopback，而且 Host／Origin 要對上允許的 scheme、host 與確切埠。權杖不進 QR、聽眾網址、`/api/setup`。
- `/api/push` 在解析表單前先做准入。同一段 single-flight。中文先廣播，英譯失敗只更新同一段。
- RoomBus 依 `room_id` 分開。缺段會標 `missing` 或對聽眾回 gap。匿名 WebSocket 不開房。
- `ResidentAsr` 的 ready 旗標不是推論。`.env` 不覆蓋已存在的行程環境。`start.bat` 與 `app.run` 共用 `BREEZE_PORT`。

## 仍未完成

- 工作樹沒提交，CI 還看不到這輪差分。
- 模型 SHA256 沒有在這裡計算，所以沒有釘選。
- 預設仍是每次重開 whisper-cli。常駐模型「只載入一次」沒有實機證據。
- 不是單一 exe／MSI。捷徑腳本沒在這裡執行。Windows DLL、防火牆、埠佔用、含中文或空白的路徑都沒驗證。
- 標點只對以「嗎／呢」結尾且尚無句末符號的句子補問號，不是完整標點還原。
- 預設字幕在記憶體。SQLite 要另設 `BREEZE_DATA_PATH`，沒有完整 migration，不存音檔。仍是單一 Uvicorn process。
- 不能把 6–15 秒、RTF、p50／p95、RAM、CPU 或「模型已在實機只載入一次」寫成量測結果。這裡沒有這些數字。

## 自動測試已通過

2026-10-05，Linux，倉庫 venv 的 CPython 3.11.17（`/root/breeze-live-room/.venv`），Node v26.3.1。工作樹、不是 GitHub Actions 結果。沒有下載模型，沒有付費金鑰。

- rebase 後的權威結果：`cd /root/breeze-live-room && .venv/bin/python -m pytest -q --tb=line` 印出 `34 passed in 14.78s`（exit 0）。
- 同一批測試較早也通過：`34 passed in 11.49s`（rebase 前）、`32 passed`、`31 passed in 10.88s`、`31 passed in 10.24s`、`31 passed in 9.74s`。
- socket `__aexit__` 與顯示順序（display-order）測試修正之前的乾淨複製是 `25 passed, 6 failed`，不能當成目前結果。
- `node tests/recorder_machine.test.mjs` 印出 `recorder machine ok`；`node tests/room_client.test.mjs` 印出 `room client ok`。

CI 工作流程指定 Node 22，這台沒有用 Node 22 重跑。GitHub Actions 還沒在這個工作樹上跑。Copilot review 成功不等於測試通過。

## 實機驗證沒有通過

30 分鐘講話、2 小時連續跑、3 台手機，以及 Ryzen 5 5600H / 16GB / Windows 11 主持機上的檢查都沒有通過。具體阻塞：這台環境跑不了該測試。不要編 RTF 或延遲。阻塞清單在 `docs/TEST-REPORT.md`。

## 主持機紀錄（不是這次量測）

2026-10-05 看過的主持機（DESKTOP-P8RGA3A，暫時用）：

- AMD Ryzen 5 5600H with Radeon Graphics，3.30 GHz
- 記憶體 16GB（可用 15.4GB），3200 MT/s
- 顯示卡是 AMD Radeon 內顯，Windows 寫 496 MB，沒有 NVIDIA
- 碟 477GB，當時已用 255GB
- Windows 11 Pro 25H2

結論：規格上能跑 q5，不能走 CUDA。延遲沒有在這台查核機上量過。這段只是看過的規格，不是實機驗證通過。

## 安裝

步驟、埠與模型網址見 `docs/INSTALL.md`。權重不進 Git。沒有可分享的區網位址時不發 QR，也不改成 localhost。

## 現在沒做到的

- 不是單一 exe／MSI，是 `install.bat` 加尚未在此執行的桌面捷徑腳本。
- 沒有 Qwen realtime 一條長連線，也沒有設定頁切換聽寫引擎。
- 熱詞只是 `initial_prompt` 偏置，不保證鎖詞。譯文用詞放翻譯層。

完整說明見 `docs/SPEC.md`。
