---
name: auth-feature-policy
description: 功能政策矩陣（3 角色 × 4 層級 × 功能鍵）的運作與修改：policies.py 的預設、seed 與一次性 remap、60 秒快取、寫入路徑映射、前端鏡像。新增功能鍵或調整某功能需要的層級時使用。
---

# 功能政策

## 模型
- 層級順序 `LEVEL_ORDER = {stream_only:0, photos_stream:1, full:2, owner_only:3}`；`owner_only` 只是政策值，意思是「被分享者永遠不能用」。
- 表 `feature_policies(feature_key UNIQUE, min_level, enabled, description, updated_by)`；預設在 `policies.py` 的 `FEATURE_DEFAULTS`（目前 12 鍵：`stream.view`、`photos.view`、`photos.download`、`timelapse.create`、`camera.settings`、`camera.control`、`camera.rename`、`camera.share`、`camera.unbind`、`camera.delete`、`device.replace`、`notify.subscribe`）。
- 只約束真正的被分享者（`granted_by != self`）；擁有者與 `symotus_admin` 豁免。
- `level_allows(feature_key, level)` 缺列時退回預設（fail-safe）；讀取快取 60 秒，改完要 `invalidate_cache()`。

## 新增功能鍵
1. `FEATURE_DEFAULTS` 加 `("<area>.<verb>", "<min_level>", "中文說明")`。`seed_policies` 啟動時只補缺列與更新說明，**不覆寫管理員改過的 `min_level`**。
2. 若要改既有鍵的預設層級且希望既有環境跟著改，加進 `_LEVEL_MIGRATIONS`（只在新程式第一次啟動時跑，判定靠 `photos.download` 是否存在，不要重用這個判定做第二次 remap，請換新的哨兵鍵並寫註解）。
3. `routers/cameras.py` 的 `feature_for_write(path)`：把新寫入路徑映射到新鍵（目前：ptz／reboot／autofocus → `camera.control`，空路徑 → `camera.rename`，replace／release-device → `device.replace`，timelapse-* → `timelapse.create`，其餘 → `camera.settings`）。
4. 前端鏡像：`src/hooks/useFeaturePolicy.ts` 的 `DEFAULT_LEVELS`、`src/lib/permissions.ts` 標籤、`PolicyTab.tsx` 顯示。前端只藏按鈕，後端才是閘門。
5. 管理 API 不用改：`GET /policies`、`PUT /policies/{feature_key}`、`POST /policies/reset` 會自動涵蓋新鍵。

## 測試
- `tests/test_policies_three_modes.py`：新鍵在三種層級各一題，加「admin 改過的值不被 seed 覆寫」一題。
- `tests/test_photos_download_gate.py` 是「寫入／下載路徑被擋」的範例，照抄。
