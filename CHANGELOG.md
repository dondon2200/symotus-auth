# Changelog

所有重要更新都記錄在這裡，格式參考 [Keep a Changelog](https://keepachangelog.com/zh-TW/1.0.0/)。

---

## [Unreleased]

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
