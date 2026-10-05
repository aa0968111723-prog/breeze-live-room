# 架構

主持頁只在本機瀏覽器拿權杖。權杖放在記憶體，不進 QR、聽眾網址或 `/api/setup`。

聽眾頁只連 `/ws/listen`。上傳、設定變更走 `/api/push`，要 `Authorization: Bearer`，而且不信任 `X-Forwarded-*`。

音訊有大小上限。每一段有 `room_id`、`session_id`、`seq`。同一組重送不會再辨識一次。全機同時只跑一個 ASR。字幕先依序號放出中文，英譯失敗只更新同一段。

辨識預設是本機 whisper-cli。這條路每次都會重新啟動程式，`/api/setup` 會標 `model_reloads_each_segment: true`。常駐模型只有在本機 `127.0.0.1` 的服務真的就緒時才算載入過，這次沒有實機驗證。

沒有區網位址時不產生 QR。
