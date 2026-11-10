---
name: auth-line-webhook-command
description: 在 LINE 官方帳號 webhook 新增或修改一個文字指令或回覆：驗章、身分解析、指令處理順序、reply 與 push 的差別、確認流程、推播去抖動、測試。動到 routers/line_webhook.py 或通知服務時使用。
---

# LINE webhook 指令

檔案：`routers/line_webhook.py`（事件入口與指令）、`routers/line_bind.py`（一鍵綁定）、`services/camera_notifier.py`（開關機通知）、`services/job_notifier.py`（任務完成通知）。

## 入口的固定順序（新指令插在正確位置）
1. 驗章：`X-Line-Signature` = base64(HMAC-SHA256(channel secret, **原始 body bytes**))，`compare_digest`。驗章前不要 `json.loads` 再 dumps。
2. 解析身分：`line_user_id` → `user_line_accounts`（一人可綁多個 LINE 帳號，`is_active` 決定目前作用中的）。未綁定者只允許綁定碼流程。
3. 指令處理順序：`_handle_bind_code`（6 碼綁定碼）→ `_handle_switch_command`（切換作用中帳號）→ `_handle_confirm_command`（「確認」執行待確認的寫入動作）→ 其餘文字轉呼叫前端 `/api/assistant`（與網頁版共用工具與防護）。
4. 回覆用 **reply token**（免費、約 1 分鐘內有效、一次最多 5 則）；逾時或非事件觸發才用 push（吃月額度）。

## 新增指令
- 寫成 `_handle_<name>(db, line_user_id, text) -> str | None`，回 `None` 表示不是你的指令，讓後面的 handler 接手。
- 指令文字比對要去除前後空白、接受全半形；中文指令給一個英文別名。
- 需要寫入相機或帳號的指令，**不要直接做**：走與 AI 助手相同的待確認流程（回摘要，使用者回「確認」才執行，單次、5 分鐘、綁 user）。
- 回覆文字裡的相機名稱等外部資料不要拼進任何會再餵模型的內容；要餵模型就包 `<untrusted>`。

## 推播（notifier）慣例
- `camera_notifier.py` 每 60 秒輪詢；相機要**連續 3 次離線**才算真的關機，之後才重新武裝下一次開機通知，避免心跳抖動重複推播。收件人依 `notify.subscribe` 政策過濾。
- `job_notifier.py` 每 120 秒同步 Spark 任務，完成才推一則。
- 任何新通知都要有去重鍵（事件 id 或「狀態轉移」），不要每輪都推。
- 媒體網址用 HMAC 簽章的 30 分鐘連結（`public_camera.py`），不要把 token 放進 URL。

## 測試
- `tests/test_line_webhook_*.py`：`bind_code`、`confirm`、`identity`、`notify_cancel`、`switch` 各有範例；新指令照 `test_line_webhook_switch.py` 的寫法，用 TestClient 直接 POST 事件 JSON 並對驗章用的 secret 簽名。
- 推播邏輯：`test_notify_gate_multi_line.py`。
- 本機聯調用 ngrok 把 webhook 指到本機，LINE Developers 後台設定；channel secret 走 env，不進 repo。
