# TEST-REPORT

續作分支：round2-core，基礎 PR #1 20d53c9。

已修：CI 用 pythonpath 與 python -m pytest；事件依 room_id 派送；解碼失敗會落定缺段並放行後續；聽眾依 segment id/version 更新，stop 會關 socket；Origin 不再信任意 Host。

自動測試：推送後由 GitHub Actions 跑。本沙盒沒裝 fastapi，不宣稱本機已通過完整 pytest。

實機：未在 Ryzen 5 5600H 跑 30 分鐘或 2 小時，也未接真實 Breeze。常駐模型仍是 CLI 降級。
