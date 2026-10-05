# 測試紀錄

日期：2026-10-05。機器：Linux 查核環境，不是 Windows 主持機。

## 已實作

- 主持權杖、無權杖拒絕上傳、權杖不進 setup / QR
- 英譯例外時中文仍留下，同一 segment id
- 同段重送不重複辨識
- 後段先辨識完，廣播仍先出序號 1
- 房間 id 不再截斷，非法代號拒絕
- 音訊超過上限回 413
- whisper-cli 非零退出不當成功
- 錄音狀態機拒絕重入，中間切片不上傳，停止後釋放計時器與軌道
- 沒有區網位址時不產生 localhost QR

## 自動測試通過

在這台 Linux 執行，2026-10-05：`pytest` 8 passed，`node tests/recorder_machine.test.mjs` 印出 recorder machine ok。沒有下載模型，沒有使用付費金鑰。

## 實機驗證通過

沒有。尚未在 Ryzen 5 5600H / 16GB / Windows 11 上跑 30 分鐘或 2 小時，也沒有 3 台手機斷線測試。不能宣稱延遲、RAM 或模型只載入一次。

## 阻塞

- 需要主持機、麥克風、Breeze q5 權重、whisper.cpp Windows 套件、ffmpeg
- 英譯 401 / 429 / 計費用量需要使用者提供的金鑰，這次沒有代填
