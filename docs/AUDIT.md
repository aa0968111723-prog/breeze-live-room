# 查核紀錄

基準：`main` `76a510bfae442d0e753d612e402d3350a09ed7d1`（2026-10-05）。查核當下 `main` 與此提交相同，沒有未合併 PR，沒有後續提交。倉庫沒有 `AGENTS.md`。

這台機器是 Linux，沒有 Windows 主持機、沒有麥克風、沒有 Breeze 權重、沒有 `whisper-cli.exe`、沒有 OpenAI 金鑰。下列「自動測試通過」只代表這台 Linux 的測試。實機驗證另列。

## 已重現

### 未授權的 /api/push

- 位置：`start.bat` 綁 `0.0.0.0`；`app/server.py` `push`
- 觸發：同一區網對 `/api/push` 送音訊，不需任何憑證
- 影響：可污染字幕，並在有金鑰時觸發英譯費用
- 優先：P0
- 修法：主持端本機核發權杖；上傳、設定、清除、匯出都要權杖。權杖不進 QR、聽眾 URL、日誌
- 驗收：無權杖回 401；權杖不出現在 `/api/setup` 與 `/api/qr`

### 英譯失敗整段消失

- 位置：`push` 把 `translate()` 放在寫入 history 之前，例外被外層吃掉
- 觸發：辨識成功後英譯丟例外
- 影響：聽眾看不到已辨識的中文
- 優先：P0
- 修法：中文先落定並廣播，英譯失敗只更新同一 segment
- 驗收：翻譯器丟例外時，history 仍有該段中文，且 id 不重複

### 同時辨識導致後段先出

- 位置：每個 POST 各自 `asyncio.to_thread(transcribe)`
- 觸發：兩段幾乎同時送入，第二段辨識較快
- 影響：字幕順序錯
- 優先：P0
- 修法：全機單一 ASR worker，並依 `seq` 對齊後才廣播
- 驗收：人為讓第一段較慢，廣播順序仍是 seq 1 再 seq 2

### 房間 id 截斷碰撞

- 位置：`room()` 只留前 32 字
- 觸發：兩個前 32 字相同、後面不同的 id
- 影響：兩個房間共用 history 與聽眾
- 優先：P1，一併在這次修
- 修法：不截斷。只接受 1–64 的英數、底線、減號，否則 400
- 驗收：長 id 不會併成同一間；非法字元被拒

### 非零退出仍可能當成功

- 位置：`transcribe()` 在 `returncode != 0` 但 stdout 有字時仍回傳文字
- 觸發：whisper-cli 失敗但印出部分 stdout
- 影響：失敗被當成正常字幕
- 優先：P0
- 修法：非零退出就是失敗，stdout 不當成功
- 驗收：單元測試回傳失敗狀態

## 靜態程式碼確認

### 重複開始錄音

- 位置：`app/static/host.html` 開始按鈕沒有鎖
- 觸發：連按兩次開始再停止
- 影響：舊計時器與舊麥克風軌道還在
- 優先：P0
- 修法：`idle / preparing / recording / draining / error`，開始前鎖定，停止與失敗都釋放
- 驗收：`node tests/recorder_machine.test.mjs`

### 每段切片不保證可獨立解碼

- 位置：`requestData(); stop(); start()` 每 6 秒
- 觸發：`dataavailable` 的中間切片
- 影響：ffmpeg 解不開，或邊界漏字、重複
- 優先：P0
- 修法：每一段用獨立的 start/stop，只上傳該段 `stop` 後的完整 Blob。不把 `requestData()` 切片當獨立檔
- 驗收：狀態機測試要求段落在 stop 之後才上傳

### 過短尾段用 800 bytes 丟棄

- 位置：`host.html` `ev.data.size<800`
- 影響：短但有效的尾段被丟
- 優先：P0
- 修法：改由伺服器依轉檔後時長判斷，不再用固定 800 bytes

### 音訊整包讀入、暫存清理不完整

- 位置：`audio.read()` 無上限；`wav` 在轉檔失敗時可能仍是 None，若 ffmpeg 寫出檔卻丟例外，finally 可能清不到
- 優先：P0
- 修法：有上限的讀取；每任務一個暫存目錄，finally 整目錄刪除

### 金鑰與日誌

- 位置：沒有 `.env.example`；`.gitignore` 沒排除 `.env`、`tmp/`、資料庫、日誌
- 優先：P0
- 修法：補 example 與 gitignore。回應只給遮罩狀態，不回金鑰

### QR 與區網位址

- 位置：`lan_ip()` 連 8.8.8.8，失敗回 127.0.0.1，仍產生 QR
- 影響：手機掃到連不上的 localhost，畫面卻像成功
- 優先：P1
- 修法：沒有區網位址就不發 QR，明示尚無可分享連結

### 聽眾斷線

- 位置：`room.html` `onclose` 只叫重新整理
- 優先：P1
- 修法：有上限的自動重連，並用最後 seq 補段

### README 延遲

- 位置：README、FULL 寫字幕大概晚 6–15 秒
- 狀態：這次查核沒有在指定硬體上量到。改成未驗證的估計，不當成實測

## 需實機驗證

- Windows 11、Ryzen 5 5600H、16GB、無 NVIDIA 上的模型載入、RTF、RAM
- 真實麥克風、連續 30 分鐘與 2 小時
- 至少 3 台手機或平板，含一次斷線恢復
- Windows 防火牆、port 佔用、含中文與空白的安裝路徑
- Breeze GGML q5 與固定版 whisper-server 的相容性。這台沒有權重，不能宣稱模型只載入一次已在實機通過
- OpenAI 401、429、計費。沒有金鑰，只用假翻譯器測故障路徑

## 建議新增

- 術語表匯入匯出、VAD、SQLite 長期保留策略的完整 migrations
- 單一 exe / MSI。核心流程穩定後再做
- 多程序協調。現在維持單一 Uvicorn process
