---
name: auth-migration
description: 改 symotus-auth 的資料庫 schema：SQL 檔命名、startup 的 create_all／ALTER 慣例、向前相容的部署順序、SQLite 測試限制、正式庫套用方式。任何新增表、欄位、索引、約束都用這個。
---

# Schema 變更

## 兩條路，別混用
1. **新表或新欄位且可用 SQLAlchemy 表達**：在 `models.py` 加 model／欄位；`main.py` 的 startup 會 `create_all` 並對既有表做 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 類補欄（看 `main.py` startup 區塊既有寫法照抄）。
2. **刪欄、改型別、搬資料、加約束或部分唯一索引**：寫 `migrations/YYYY-MM-DD_<snake_name>.sql`（例：`2026-10-26_users_jobs_view_all.sql`），第一行註解寫「為什麼」與對應的 Task／spec，SQL 要**可重跑**（`IF NOT EXISTS`、`IF EXISTS`）。套用方式見 `symotus-auth_DEPLOYMENT.md`；套用後把檔案留在 repo 當紀錄。

## 向前相容的順序（正式站 push 即部署，沒有 staging）
1. 先加欄位（nullable 或有 default），不碰舊欄位 → 部署。
2. 程式改成同時寫新舊欄位、讀新欄位 → 部署。
3. 回填舊資料（`scripts/` 放一次性腳本，寫法見 `scripts/backfill_gdrive_job_names.py`，要有 `--dry-run`）。
4. 加約束、移除舊欄位 → 部署。
不要在同一版同時改 schema 與改語意；migration 失敗時站台還要能跑。

## 常用型別慣例
- 金額：整數最小單位或 `NUMERIC`，不用 float；百分比用整數基點（`commission_percent_bps`，1250 = 12.5%）。
- 時間：`timestamptz`，寫入 UTC；naive 時間是災難來源。
- 狀態：字串 enum 加 CHECK，或在程式層以常數集合檢查；狀態機規則寫在 service（見 `services/billing_v2.py`）。
- 「同一期只能有一張有效」這種規則用**部分唯一索引**：`CREATE UNIQUE INDEX ... ON t (a, b) WHERE status <> 'void'`，既是約束也是小索引。
- 大表加索引用 `CREATE INDEX CONCURRENTLY`（不能在 transaction 裡）。

## SQLite 測試的限制
- `tests/conftest.py` 只建 `_TABLES` 列出的表；PostgreSQL 專屬的 `ARRAY`、部分索引語法在 SQLite 可能建不起來。新 model 加進 `_TABLES`，無法建的欄位用 JSON 或在測試 mock。
- 鎖機狀態有 10 秒程序內快取，conftest 的 autouse fixture 每題都 `invalidate_lock_cache()`；寫新快取時比照做。

## 完成檢查
- [ ] `pytest -q` 全綠
- [ ] 本機 `docker compose up -d --build` 啟動一次，確認 startup 的 create_all／ALTER 不炸
- [ ] migration SQL 可重跑；部署順序寫進 PR 描述
