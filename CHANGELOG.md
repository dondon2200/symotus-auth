# Changelog

所有重要更新都記錄在這裡，格式參考 [Keep a Changelog](https://keepachangelog.com/zh-TW/1.0.0/)。

---

## [Unreleased]

### Added
- **縮時影片公開分享**（2026-10-14，`routers/jobs.py`、`auth.py`）
  - `POST /jobs/share {kind, job_id, days?}`：對已完成任務簽到期 ticket（預設 7 天、最長 30），回 `{url: FRONTEND_URL/video/{token}, expires_at}`；擁有者或 symotus_admin 才能簽
  - `GET /jobs/public/{token}` 回中繼資料、`GET /jobs/public/{token}/video[?download=1]` 串流自 Spark（支援 Range），皆不需登入；任務刪除或 ticket 過期即 404
  - 影片串流抽成 `_stream_spark_video()`，GDrive 下載與公開分享共用
- **縮時任務完成／失敗 LINE 通知與背景同步**（2026-10-14，`services/job_notifier.py`）
  - 任務轉終態（列表同步、PUT 查證、GDrive 輪詢、Spark 回呼）時推 LINE 給建立者所有 `is_active` 綁定；同任務 10 分鐘內只推一次；完成時間早於 24 小時不推
  - 每 `JOB_SYNC_INTERVAL`（預設 120 秒）把未結束的相機任務與已送 Spark 的 GDrive 任務向 Spark 對齊一次，使用者不開任務頁也會轉態並收到通知
  - 前端舊的「完成時通知我」按鈕打的 `/cameras/notify-job-complete` 從未存在，隨此一併移除
- **任務列表帶建立者**：`GET /jobs`、`GET /jobs/gdrive` 每筆多 `owner_id`、`owner_name`（full_name 優先，否則 username）

### Changed
- **縮時任務對所有 symotus_admin 可見**（2026-10-14，`routers/jobs.py`）
  - 新增 `_scope_to_user()`：symotus_admin 不套 `user_id` 過濾，其他角色維持只看自己的
  - 套用於 `GET/PUT/DELETE /jobs`、`GET /jobs/gdrive`、`GET/POST(resume)/DELETE /jobs/gdrive/{id}`；影片下載 ticket 原本就綁任務擁有者，不用改
  - 原因：jimmy_admin 登入後看不到 Super Admin 建的 Google Drive 縮時任務；管理員本就能操作全部相機，任務比照辦理
  - 測試：`tests/test_jobs_admin_visibility.py`

### Added
- **縮時送出前分析（precheck）輪詢代理**（2026-10-08，`routers/cameras.py`）
  - 新增 `GET /timelapse-prechecks/{precheck_id}` 轉發 Camera Backend 同名端點；先用使用者自己的 token，被拒再退 admin token（送出若是用 admin token 送的，CB 用使用者 token 查會 404）
  - `policies.py`：`timelapse-precheck` 歸入 `timelapse.create`（原本會落到 camera.settings，被分享者 403）

---

## [2026-06-02 ~ 2026-06-03]

### Fixed
- **`nas/images` endpoint 每次 500 crash** (`routers/cameras.py`)
  - 原因：`asyncio.gather` 使用但頂部缺少 `import asyncio`
  - 補上 import，日期資料夾並行查詢功能正式生效

- **`limit` 超過 30 時回傳 0 筆** (`routers/cameras.py`)
  - Camera Backend 單次最多回傳 30 筆，請求 `limit=150` 時直接回傳空陣列
  - 改為分批查詢：`CAM_MAX = 30`，loop 直到收集足夠筆數

- **Render 免費版 OOM 崩潰** (`routers/cameras.py`)
  - `asyncio.gather` 對 365 天資料夾一次發 365+ 個請求，耗盡 512MB 記憶體
  - 加 `asyncio.Semaphore(10)`：最多同時 10 個並發請求

### Added
- **Admin 撤銷任意支援授權** (`routers/support.py`)
  - 新增 `DELETE /support/admin/grants/{grant_id}`（需 `symotus_admin` 角色）
  - 原有的 `DELETE /support/grants/{id}` 只允許 reseller 撤銷自己的授權，admin 無法操作
  - 新端點讓 admin 可以強制撤銷任何 reseller 的授權

### Infrastructure
- `docker-compose.yml`：正式環境版，拉 GHCR 映像（`ghcr.io/dondon2200/symotus-auth:latest`）
- `Dockerfile`：python:3.11-slim，讀 PORT 環境變數，PORT=8001
- `.github/workflows/ci-cd.yml`：lint → smoke-test → release → deploy 四段式 CI/CD
- `docker-compose.ci.yml`：僅供 GitHub Actions smoke test 使用
- `ruff.toml`：忽略 E711/E712/E402 等 SQLAlchemy 正常寫法
- 部署目標：`https://auth.symotus.com`（尚未切換，目前仍在 Render）
