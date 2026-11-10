---
name: auth-billing-v2-change
description: 修改計費 v2（每台相機訂閱、週年帳單日、逾期處置、付款回報、鎖機）的任何規則前先讀這個：規格與程式位置、日期算法、冪等與鎖、排程上限、鎖機閘門、測試。
---

# 計費 v2 變更

## 位置
- 規格：`docs/superpowers/specs/2026-10-07-camera-subscription-billing-design.md`（在 frontend repo）。改規則先改規格再改程式。
- 程式：`routers/billing.py`（API）、`services/billing_v2.py`（規則與狀態機）、`services/billing_dates.py`（日期）。
- 表：`billing_plans_v2`、`billing_subscriptions_v2`、`billing_bills`（`UNIQUE(subscription_id, period_start)`）、`billing_payment_reports`、`billing_payment_report_bills`（含 `amount_snapshot`）、`billing_payment_receipts`、`billing_settings`。
- v1（用量計量制、`billing_usage_daily`、整數基點分潤）已於 2026-10-07 `dd31f78` 被取代，只在 git 歷史；`docs/BILLING_PM_GUIDE.md` 是 v1 的手冊，已標歷史。

## 不可違反的規則
1. **帳單日期永遠是 `start + n 個月`，日取 `min(錨定日, 該月天數)`**；絕不從上一張帳單推算（1/31 → 2/28 → 3/28 會漂移）。測試在 `tests/test_billing_dates.py`。
2. 訂閱狀態機 `scheduled → active ⇄ suspended → ended`，`end_reason ∈ {not_renewed, terminated, non_payment}`；帳單 `unpaid / paid / void`。新狀態先改規格與狀態機表，再改程式。
3. 排程（每日台北 00:10 與啟動時）`advance_subscription` 補齊到今天為止的帳單；單次最多 `MAX_NEW_BILLS_PER_RUN = 24`，撞唯一鍵視為「別人已建」。不要移除上限。
4. 付款：帳單列 `SELECT ... FOR UPDATE`；已在待審報告的帳單再報回 409 `BILL_IN_PENDING_REPORT`；憑證只收 jpg／png／pdf、2 MB，檢查檔頭不信副檔名；admin 可部分確認；回應含 `difference = amount − bills_total`。
5. 鎖機：`assert_camera_unlocked` 回 403 `CAMERA_LOCKED`；`get_lock_map` 快取 10 秒；計費查詢本身失敗時 **fail-open**（記 log 放行，不因計費壞掉讓工地看不到畫面）。鎖定的相機只剩 admin 可進。
6. 金額整數或 NUMERIC；百分比整數基點、`ROUND_HALF_UP`。

## 改完要過的測試
- `tests/test_billing_v2_api.py`、`test_billing_v2_reports.py`、`test_billing_v2_schedule.py`、`test_billing_dates.py`。
- `tests/test_billing_v2_lock_gate.py`：盤點所有 `{camera_id}` 路由都有閘門或登記豁免。
- conftest 每題都清鎖機快取；自己加的快取也要清。

## 前端對應
- `src/lib/billing.ts`、`billingDates.ts`、`billingTypes.ts`、`billingLock.test.tsx`；後台頁 `src/app/(app)/admin/billing/*`（10 頁）；旗標 `NEXT_PUBLIC_BILLING_ENABLED`。
