---
name: auth-test-and-ship
description: symotus-auth 的本機測試、啟動與出貨流程：pytest 與 SQLite 限制、docker compose 本機驗證、CI 只建映像不跑測試、祕鑰先於程式、觸發前端部署、健康檢查。每次要 commit 或 push auth 前使用。
---

# 測試與出貨（auth）

## 本機測試（唯一的測試關卡：CI 不跑 pytest）
```bash
pip install -r requirements-dev.txt
pytest -q                      # 約 46 個檔、480+ 題，SQLite，數十秒
pytest tests/test_billing_v2_lock_gate.py -q   # 改到相機路由時單獨先跑
```
- `tests/conftest.py` 用 SQLite 自組最小 app；只建 `_TABLES` 的表，新 model 要加；`ARRAY` 等 PostgreSQL 型別建不起來。
- 每題自動清鎖機快取；時間相關邏輯把 `now` 當參數注入，不在函式內 `datetime.now()`。

## 本機啟動
```bash
docker compose up -d --build                     # auth-service:8001 + postgres:5433
docker compose exec auth-service python init_admin.py   # 第一個 symotus_admin
curl -s localhost:8001/health
```
整套（edge + frontend + auth + db）用 `symotus-local-deploy/docker-compose.yml`（HTTP edge:8088）。

## 出貨順序（正式站 push 即部署）
1. 簡述變更並**請求允許**；未獲允許不 push。
2. **涉及新祕鑰或 env**：先在 `symotus-frontend` repo 的 GitHub Secrets／Variables 設好（對照 `docs/DEPLOY_CONFIG_CHECKLIST.md`），再推 auth。順序反了會讓新映像在 secret 未就緒時上線。
3. `git add` → `git commit -m "<type>(<scope>): <摘要>"`（結尾 Co-Authored-By）→ `git fetch origin && git rebase origin/main` → `git push origin main`。
4. CI（`.github/workflows/build.yml`）建映像推 GHCR，再以 `DEPLOY_DISPATCH_TOKEN` 發 `repository_dispatch deploy` 給前端 repo；沒設 token 就手動重跑前端 deploy。
5. 驗證：`curl -s https://user.symotus.com/auth-api/health` 期望 `{"status":"ok"}`；有 migration 的要再打一次相關 API 確認。
6. 失敗先看站台是否 502；復原方式見前端 repo 的 `fe-test-and-ship`。

## 部署後的背景工作
- startup 會啟動三個背景 loop：`start_camera_notifier`（60 秒）、`start_billing_scheduler`（台北 00:10 與啟動時）、`start_job_sync_loop`（120 秒）；task 物件要存在模組層級變數，否則被 GC 後任務無聲消失（`main.py` 有註解）。
- Google Drive 下載跑在 uvicorn 行程內，**每次部署都會把下載中任務打成 interrupted**（已知 P1），部署前看一下有沒有進行中的大任務。
- 同機是正式 Camera Backend 共用主機，只碰 `symotus-edge/-auth/-auth-db`。
