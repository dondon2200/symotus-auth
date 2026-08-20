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


def test_資料庫層直接擋下同相機重複生效中訂閱(db, make_user):
    """router 端只做 SELECT-then-INSERT 檢查，並發下擋不住。這裡不透過 API，
    直接對 DB 塞兩筆同 camera_id、status=active 的訂閱列，驗證『部分唯一索引』
    本身真的存在且會擋下重複列。"""
    from sqlalchemy.exc import IntegrityError
    from models import BillingSubscription

    customer = make_user("idx_customer", "idx_customer@test.com", role="reseller")

    from models import BillingPlan
    p = BillingPlan(name="索引測試方案", monthly_fee=100)
    db.add(p); db.commit(); db.refresh(p)

    db.add(BillingSubscription(camera_id=999, customer_id=customer.id, plan_id=p.id, status="active"))
    db.commit()
    db.add(BillingSubscription(camera_id=999, customer_id=customer.id, plan_id=p.id, status="active"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_GET客戶清單不寫入DB且用預設值(client, admin, reseller, auth_headers, db):
    """list_customers 是 GET，不該有寫入副作用。舊版對每個使用者呼叫
    get_or_create_customer(內含 commit)，這裡驗證呼叫過一次 GET 之後
    billing_customers 仍是空的，但回應內容用預設值(billing_day=1, frozen=False,
    note=None)正確組出。"""
    from models import BillingCustomer

    assert db.query(BillingCustomer).count() == 0
    r = client.get("/billing/admin/customers", headers=auth_headers(admin))
    assert r.status_code == 200
    me = [c for c in r.json() if c["user_id"] == reseller.id][0]
    assert me["billing_day"] == 1
    assert me["frozen"] is False
    assert me["note"] is None
    assert db.query(BillingCustomer).count() == 0


def test_凍結不存在的使用者回404(client, admin, auth_headers):
    r = client.post("/billing/admin/customers/999999/freeze", headers=auth_headers(admin))
    assert r.status_code == 404


def test_解除凍結不存在的使用者回404(client, admin, auth_headers):
    r = client.post("/billing/admin/customers/999999/unfreeze", headers=auth_headers(admin))
    assert r.status_code == 404
