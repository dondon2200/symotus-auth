---
name: auth-new-endpoint
description: 在 symotus-auth 新增一個 FastAPI 端點或 router：相依注入、角色與相機權限閘門、計費鎖機閘門、錯誤碼格式、main.py 的掛載順序、測試。任何新 API 都用這個。
---

# 新增端點

## 放哪裡
- 既有領域就加在對應 router（`routers/auth.py`、`cameras.py`、`jobs.py`、`billing.py`、`invitations.py`、`line_bind.py`、`line_webhook.py`、`admin.py`、`users.py`、`support.py`、`public_camera.py`）。
- 新領域才開新檔，並在 `main.py` 底部 `app.include_router(...)`。**順序有意義**：`public_camera` 必須在 `cameras` 之前（`/cameras/{id}/{path}` 是 catch-all 代理）；不在 `/cameras/` 底下的 Camera Backend 端點要另開 router（例：`cameras.precheck_router` 的 `GET /timelapse-prechecks/{id}`）。

## 相依注入
```python
from database import get_db
from auth import get_current_user, require_role
@router.get("/things/{camera_id}")
def list_things(camera_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
```
- `get_current_user` 每請求重讀 DB（停用立即生效），不要快取使用者。
- 只限角色：`Depends(require_role("symotus_admin"))`。
- 對外公開的端點（分享連結、公開影片）放 `public_camera.py`／`jobs.py` 的 public 區，用 HMAC 簽章或 token，不吃 session。

## 閘門（缺一個就是資安洞）
1. **相機存取**：先確認使用者對該 `camera_id` 有權（擁有、`camera_access` 授權、或 admin）。代理到 Camera Backend 時 token 順序是「自己 → 授權者 → admin」，admin fallback 只在有授權或本人是 admin 時允許（資安審查 F-3）。
2. **寫入的功能層級**：寫入路徑要經 `feature_for_write(path)` 映射到功能鍵並用 `policies.level_allows` 檢查（F-5）。
3. **計費鎖機**：任何路徑含 `{camera_id}` 的端點，要呼叫 `services.billing_v2.assert_camera_unlocked(db, user, camera_id)`，或在 `tests/test_billing_v2_lock_gate.py` 的 `EXEMPT` 登記並寫理由。該測試會盤點所有相機路由，漏掉就紅。
4. **限流**：登入／註冊／交換類端點用既有的記憶體限流 helper（login 10/min、register 5/min、exchange 30/min 的同一套）。

## 錯誤回應
- 用 `HTTPException(status, detail={"code": "SNAKE_CASE", "message": "…"})` 的既有格式；前端只認 `code`。例：`CAMERA_LOCKED`（403）、`BILL_IN_PENDING_REPORT`（409）。
- 不要把例外字串回給客戶端（F-10）；log 裡寫詳細，回應寫通用。
- 404 與 403 的選擇：對方不該知道資源存在就回 404。

## 稽核
- 會改變帳號、權限、計費、相機綁定的動作寫 `audit.py` 的 audit log（誰、何時、改什麼、前後值）。

## 測試（本機一定要跑，CI 不跑 pytest）
- `tests/conftest.py` 用 SQLite 自組最小 app，只建 `_TABLES` 列出的表：新增 model 要把 `__table__` 加進去；PostgreSQL 專屬型別（`ARRAY`）在 SQLite 建不起來，要避開或在測試裡 mock。
- 新端點至少三題：正常、無權限（403／404）、閘門（鎖機或層級不足）。
- 指令：`pip install -r requirements-dev.txt && pytest -q`。
