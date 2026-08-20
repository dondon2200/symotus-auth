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


def test_自訂月費覆蓋訂閱列表顯示的月費(client, admin, reseller, auth_headers, db):
    """/admin/subscriptions 和 /subscriptions/my 的 monthly_fee 過去直接顯示方案原價，
    忽略客戶的 custom_monthly_fee，跟真正開出來的發票金額（同用 effective_monthly_fee）
    對不上。這裡驗證兩個端點都要顯示「客戶實付」，且與發票明細金額一致。"""
    from datetime import datetime
    from models import BillingSubscription

    h = auth_headers(admin)
    pid = _plan_id(client, h)
    client.post("/billing/admin/subscriptions",
                json={"camera_id": 99, "customer_id": reseller.id, "plan_id": pid}, headers=h)
    client.put(f"/billing/admin/customers/{reseller.id}", headers=h, json={"custom_monthly_fee": 300})

    admin_subs = client.get("/billing/admin/subscriptions", headers=h).json()
    sub = [s for s in admin_subs if s["camera_id"] == 99][0]
    assert sub["monthly_fee"] == 300

    my_subs = client.get("/billing/subscriptions/my", headers=auth_headers(reseller)).json()
    my_sub = [s for s in my_subs if s["camera_id"] == 99][0]
    assert my_sub["monthly_fee"] == 300

    # 跟真正開出來的發票明細金額比對
    for s in db.query(BillingSubscription).filter(BillingSubscription.camera_id == 99).all():
        s.started_at = datetime(2026, 1, 1)
    db.commit()
    r = client.post("/billing/admin/invoices/generate/2026-08", headers=h)
    assert r.json()["created"] == 1
    inv = client.get("/billing/admin/invoices?period=2026-08", headers=h).json()[0]
    assert inv["total"] == 300


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


def test_客戶條件的預設值(client, admin, reseller, auth_headers):
    h = auth_headers(admin)
    customers = client.get("/billing/admin/customers", headers=h).json()
    me = [c for c in customers if c["user_id"] == reseller.id][0]
    assert me["payment_method"] == "monthly_transfer"   # 預設月結匯款
    assert me["statement_day"] == 1
    assert me["custom_monthly_fee"] is None             # None = 用方案月費
    assert me["commission_type"] is None
    assert me["commission_percent_bps"] is None
    assert me["commission_fixed_amount"] is None


def test_更新客戶條件(client, admin, reseller, auth_headers):
    h = auth_headers(admin)
    r = client.put(f"/billing/admin/customers/{reseller.id}", headers=h, json={
        "payment_method": "credit_card", "statement_day": 25,
        "custom_monthly_fee": 800, "commission_type": "percent", "commission_percent_bps": 1500,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["payment_method"] == "credit_card"
    assert body["statement_day"] == 25
    assert body["custom_monthly_fee"] == 800
    assert body["commission_type"] == "percent"
    assert body["commission_percent_bps"] == 1500


def test_局部更新不會清掉沒帶的欄位(client, admin, reseller, auth_headers):
    h = auth_headers(admin)
    client.put(f"/billing/admin/customers/{reseller.id}", headers=h,
               json={"custom_monthly_fee": 800, "commission_type": "fixed", "commission_fixed_amount": 500})
    # 只改收款日，其他條件必須留著
    client.put(f"/billing/admin/customers/{reseller.id}", headers=h, json={"billing_day": 10})
    me = [c for c in client.get("/billing/admin/customers", headers=h).json()
          if c["user_id"] == reseller.id][0]
    assert me["billing_day"] == 10
    assert me["custom_monthly_fee"] == 800
    assert me["commission_type"] == "fixed"
    assert me["commission_fixed_amount"] == 500


def test_自訂月費可設為零(client, admin, reseller, auth_headers):
    # 0 是有效值（談成免費），必須存得進去且讀得回來，不能被當成未設定
    h = auth_headers(admin)
    client.put(f"/billing/admin/customers/{reseller.id}", headers=h, json={"custom_monthly_fee": 0})
    me = [c for c in client.get("/billing/admin/customers", headers=h).json()
          if c["user_id"] == reseller.id][0]
    assert me["custom_monthly_fee"] == 0


def test_對帳日限制在1到28(client, admin, reseller, auth_headers):
    r = client.put(f"/billing/admin/customers/{reseller.id}",
                   headers=auth_headers(admin), json={"statement_day": 31})
    assert r.status_code == 422


def test_付款方式只接受已知值(client, admin, reseller, auth_headers):
    r = client.put(f"/billing/admin/customers/{reseller.id}",
                   headers=auth_headers(admin), json={"payment_method": "bitcoin"})
    assert r.status_code == 422


def test_分潤類型只接受已知值(client, admin, reseller, auth_headers):
    r = client.put(f"/billing/admin/customers/{reseller.id}",
                   headers=auth_headers(admin), json={"commission_type": "mystery"})
    assert r.status_code == 422


def test_分潤數值不可為負(client, admin, reseller, auth_headers):
    r = client.put(f"/billing/admin/customers/{reseller.id}",
                   headers=auth_headers(admin), json={"commission_type": "percent", "commission_percent_bps": -5})
    assert r.status_code == 422


def test_分潤上限為一萬bps(client, admin, reseller, auth_headers):
    h = auth_headers(admin)
    # 10000 bps = 100%，是合法上限
    ok = client.put(f"/billing/admin/customers/{reseller.id}", headers=h,
                    json={"commission_type": "percent", "commission_percent_bps": 10000})
    assert ok.status_code == 200
    # 超過 100% 沒有商業意義，應該被擋下
    bad = client.put(f"/billing/admin/customers/{reseller.id}", headers=h,
                     json={"commission_type": "percent", "commission_percent_bps": 10001})
    assert bad.status_code == 422


def test_客戶回應帶人看得懂的分潤字串(client, admin, reseller, auth_headers):
    h = auth_headers(admin)
    client.put(f"/billing/admin/customers/{reseller.id}", headers=h,
               json={"commission_type": "percent", "commission_percent_bps": 1250})
    me = [c for c in client.get("/billing/admin/customers", headers=h).json()
          if c["user_id"] == reseller.id][0]
    assert me["commission_percent_bps"] == 1250
    assert me["commission_display"] == "12.5%"


def test_未設分潤的客戶顯示字串為空(client, admin, reseller, auth_headers):
    me = [c for c in client.get("/billing/admin/customers", headers=auth_headers(admin)).json()
          if c["user_id"] == reseller.id][0]
    assert me["commission_display"] == ""


def test_自訂月費不可為負(client, admin, reseller, auth_headers):
    r = client.put(f"/billing/admin/customers/{reseller.id}",
                   headers=auth_headers(admin), json={"custom_monthly_fee": -100})
    assert r.status_code == 422
