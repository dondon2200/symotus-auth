import pytest
from fastapi import FastAPI

from routers.billing import router as billing_router

PLAN = {"name": "標準方案", "monthly_fee": 1200, "timelapse_quota_secs": 3600, "storage_quota_gb": 100}


@pytest.fixture()
def app():
    a = FastAPI()
    a.include_router(billing_router)
    return a


@pytest.fixture()
def admin(make_user):
    return make_user("sub_admin", "sub_admin@test.com", role="symotus_admin")


@pytest.fixture()
def reseller(make_user):
    return make_user("sub_reseller", "sub_reseller@test.com", role="reseller")


def _plan_id(client, headers):
    return client.post("/billing/admin/plans", json=PLAN, headers=headers).json()["id"]


def test_指派相機到方案(client, admin, reseller, auth_headers):
    h = auth_headers(admin)
    pid = _plan_id(client, h)
    r = client.post("/billing/admin/subscriptions",
                    json={"camera_id": 42, "customer_id": reseller.id, "plan_id": pid}, headers=h)
    assert r.status_code == 200
    assert r.json()["camera_id"] == 42
    assert r.json()["status"] == "active"


def test_同一台相機不可重複訂閱(client, admin, reseller, auth_headers):
    h = auth_headers(admin)
    pid = _plan_id(client, h)
    body = {"camera_id": 42, "customer_id": reseller.id, "plan_id": pid}
    client.post("/billing/admin/subscriptions", json=body, headers=h)
    r = client.post("/billing/admin/subscriptions", json=body, headers=h)
    assert r.status_code == 409


def test_取消訂閱是軟性的(client, admin, reseller, auth_headers):
    h = auth_headers(admin)
    pid = _plan_id(client, h)
    sub = client.post("/billing/admin/subscriptions",
                      json={"camera_id": 42, "customer_id": reseller.id, "plan_id": pid}, headers=h).json()
    assert client.delete(f"/billing/admin/subscriptions/{sub['id']}", headers=h).status_code == 200

    all_subs = client.get("/billing/admin/subscriptions", headers=h).json()
    assert len(all_subs) == 1                      # 保留歷史，不是真刪
    assert all_subs[0]["status"] == "cancelled"

    # 取消後同一台相機可以重新訂閱
    r = client.post("/billing/admin/subscriptions",
                    json={"camera_id": 42, "customer_id": reseller.id, "plan_id": pid}, headers=h)
    assert r.status_code == 200


def test_reseller不能管理訂閱(client, reseller, auth_headers):
    r = client.get("/billing/admin/subscriptions", headers=auth_headers(reseller))
    assert r.status_code == 403


def test_客戶設定預設值與更新(client, admin, reseller, auth_headers):
    h = auth_headers(admin)
    customers = client.get("/billing/admin/customers", headers=h).json()
    me = [c for c in customers if c["user_id"] == reseller.id][0]
    assert me["billing_day"] == 1
    assert me["frozen"] is False

    r = client.put(f"/billing/admin/customers/{reseller.id}",
                   json={"billing_day": 15, "note": "月結 30 天"}, headers=h)
    assert r.status_code == 200
    assert r.json()["billing_day"] == 15


def test_收款日必須在1到28之間(client, admin, reseller, auth_headers):
    # 29-31 在部分月份不存在，會讓排程邏輯出現找不到日期的邊界
    r = client.put(f"/billing/admin/customers/{reseller.id}",
                   json={"billing_day": 31}, headers=auth_headers(admin))
    assert r.status_code == 422


def test_凍結與解除凍結(client, admin, reseller, auth_headers):
    h = auth_headers(admin)
    assert client.post(f"/billing/admin/customers/{reseller.id}/freeze", headers=h).status_code == 200
    customers = client.get("/billing/admin/customers", headers=h).json()
    assert [c for c in customers if c["user_id"] == reseller.id][0]["frozen"] is True

    assert client.post(f"/billing/admin/customers/{reseller.id}/unfreeze", headers=h).status_code == 200
    customers = client.get("/billing/admin/customers", headers=h).json()
    assert [c for c in customers if c["user_id"] == reseller.id][0]["frozen"] is False
