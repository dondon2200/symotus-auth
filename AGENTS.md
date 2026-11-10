# symotus-auth — 給 AI 助手與新進工程師

這是 Symotus 平台的**認證、權限與計費中心**（FastAPI + SQLAlchemy + PostgreSQL 16）。所有 Camera Backend（`user.symotus.com`，外部黑箱）的相機操作都經由這裡代理，權限只在這裡判斷一次。

# 語言規範（必守）

本專案所有回報、溝通、提問、進度說明、計畫與文件、commit 訊息、程式碼註解一律使用**繁體中文（台灣用語）**；不使用簡體字、日文，也不整段用英文。程式碼、指令、檔名、API 路徑、錯誤訊息原文與專有技術名詞可保留英文。

## 先讀
- 全系統架構、帳號層級模型、部署現況、雷區：`../symotus-frontend/docs/AI_DEV_GUIDE.md`（跨 repo 的權威上手文件）。
- 本 repo 部署：`symotus-auth_DEPLOYMENT.md`。計費 v2 規格：`../symotus-frontend/docs/superpowers/specs/2026-10-07-camera-subscription-billing-design.md`。
- `README.md` 的部署與 env 段落是舊的（Render／Railway 已下線，現為自架 docker compose），以 AI_DEV_GUIDE 為準；README 裡出現的任何金鑰字串都視為已洩漏，不要再用。

## 地圖
| 想找 | 看這裡 |
|---|---|
| 登入、refresh token 輪替、一次性碼交換 | `routers/auth.py`、`auth.py` |
| 相機代理與權限閘門、`feature_for_write` | `routers/cameras.py` |
| 功能政策矩陣 | `policies.py` |
| 計費 v2 | `routers/billing.py`、`services/billing_v2.py`、`services/billing_dates.py` |
| LINE webhook、綁定、通知 | `routers/line_webhook.py`、`routers/line_bind.py`、`services/camera_notifier.py`、`services/job_notifier.py` |
| 縮時任務、Google Drive 匯入（可續傳） | `routers/jobs.py` |
| 公開分享（HMAC 簽章媒體網址） | `routers/public_camera.py` |
| 模型、schema、稽核 | `models.py`、`schemas.py`、`audit.py`、`migrations/` |
| 測試 | `tests/`（SQLite，`conftest.py` 自組最小 app） |

## 規則
1. 相機權限只在這裡判斷；代理 token 順序「自己 → 授權者 → admin」，admin fallback 要先查授權（資安審查 F-3）。
2. 任何含 `{camera_id}` 的路由都要過計費鎖機閘門或登記豁免，`tests/test_billing_v2_lock_gate.py` 會盤點。
3. 祕鑰一律 env；錯誤回應用 `{code, message}`，不回例外字串。
4. 金額整數或 NUMERIC；時間存 UTC；帳單日期用「起始日 + n 月」。
5. CI 只建映像不跑測試：**push 前本機 `pytest -q` 必須全綠**。
6. 未獲使用者允許不 push；涉及祕鑰先設 GitHub secret 再推。

## Repo-local skills（`.claude/skills/`）
做下列事情前先讀對應的 SKILL.md，它寫了要改哪些檔、要過哪些測試、常踩的坑：
- `auth-new-endpoint`：新增端點或 router。
- `auth-migration`：改 schema。
- `auth-feature-policy`：新增或調整功能鍵。
- `auth-line-webhook-command`：LINE 指令與通知。
- `auth-billing-v2-change`：計費規則。
- `auth-test-and-ship`：測試、本機啟動、出貨。
前端對應的 skills 在 `../symotus-frontend/.claude/skills/`。
