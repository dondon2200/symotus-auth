"""方案 CRUD 與權限。

deny path 一定要測：這類錯誤在正常操作下不會顯現——前端不顯示按鈕，
不代表 API 擋得住直接呼叫。舊版就是只靠前端 gate。
"""
import pytest
from fastapi import FastAPI

from routers.billing import router as billing_router


@pytest.fixture()
def app():
    a = FastAPI()
    a.include_router(billing_router)
    return a


@pytest.fixture()
def admin(make_user):
    return make_user("bill_admin", "bill_admin@test.com", role="symotus_admin")


@pytest.fixture()
def reseller(make_user):
    return make_user("bill_reseller", "bill_reseller@test.com", role="reseller")


PLAN = {"name": "標準方案", "monthly_fee": 1200, "timelapse_quota_secs": 3600, "storage_quota_gb": 100}


def test_admin可以建立方案(client, admin, auth_headers):
    r = client.post("/billing/admin/plans", json=PLAN, headers=auth_headers(admin))
    assert r.status_code == 200
    assert r.json()["name"] == "標準方案"
    assert r.json()["monthly_fee"] == 1200
    assert r.json()["is_active"] is True


def test_reseller不能建立方案(client, reseller, auth_headers):
    r = client.post("/billing/admin/plans", json=PLAN, headers=auth_headers(reseller))
    assert r.status_code == 403


def test_未登入不能讀方案(client):
    r = client.get("/billing/admin/plans")
    assert r.status_code in (401, 403)


def test_預設只回啟用中的方案(client, admin, auth_headers):
    h = auth_headers(admin)
    created = client.post("/billing/admin/plans", json=PLAN, headers=h).json()
    client.delete(f"/billing/admin/plans/{created['id']}", headers=h)

    assert client.get("/billing/admin/plans", headers=h).json() == []
    # 停用是軟刪，帶 include_inactive 就找得回來
    back = client.get("/billing/admin/plans?include_inactive=1", headers=h).json()
    assert len(back) == 1
    assert back[0]["is_active"] is False


def test_停用後可以重新啟用(client, admin, auth_headers):
    """PUT /admin/plans 舊版用 PlanCreate 當 body schema，沒有 is_active 欄位，
    pydantic 會把前端傳的 is_active:true 直接丟掉，導致停用的方案永遠回不來。
    改用 PlanUpdate（多了 Optional[bool] is_active）修正後，重新啟用要能生效，
    且重新啟用的方案要出現在預設（僅列啟用中）的清單裡。"""
    h = auth_headers(admin)
    created = client.post("/billing/admin/plans", json=PLAN, headers=h).json()
    client.delete(f"/billing/admin/plans/{created['id']}", headers=h)
    assert client.get("/billing/admin/plans", headers=h).json() == []

    r = client.put(f"/billing/admin/plans/{created['id']}",
                   json={**PLAN, "is_active": True}, headers=h)
    assert r.status_code == 200
    assert r.json()["is_active"] is True

    back = client.get("/billing/admin/plans", headers=h).json()
    assert len(back) == 1
    assert back[0]["id"] == created["id"]


def test_修改方案(client, admin, auth_headers):
    h = auth_headers(admin)
    created = client.post("/billing/admin/plans", json=PLAN, headers=h).json()
    r = client.put(f"/billing/admin/plans/{created['id']}",
                   json={**PLAN, "monthly_fee": 1500}, headers=h)
    assert r.status_code == 200
    assert r.json()["monthly_fee"] == 1500


def test_改不存在的方案回404(client, admin, auth_headers):
    r = client.put("/billing/admin/plans/9999", json=PLAN, headers=auth_headers(admin))
    assert r.status_code == 404
