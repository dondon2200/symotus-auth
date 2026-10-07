"""計費 v2 admin API：方案、訂閱、處置、帳單、逾期處置、權限 deny path。"""
from datetime import timedelta

import pytest
from fastapi import FastAPI

import routers.billing as billing_mod
from models import BillingBill, BillingSubscriptionV2
from routers.billing import router as billing_router
from services.billing_dates import taipei_today
from services.billing_v2 import get_lock_map, invalidate_lock_cache

TODAY = taipei_today()


@pytest.fixture()
def app():
    a = FastAPI()
    a.include_router(billing_router)
    return a


@pytest.fixture(autouse=True)
def _no_camera_backend(monkeypatch):
    async def fake_resolve(cid):
        return f"cam{cid}", f"SER{cid}"

    async def fake_all():
        return [{"id": 44, "name": "cam44"}, {"id": 45, "name": "cam45"}, {"id": 46, "name": "cam46"}]

    monkeypatch.setattr(billing_mod, "_resolve_camera", fake_resolve)
    monkeypatch.setattr(billing_mod, "_fetch_all_cameras", fake_all)


@pytest.fixture()
def admin(make_user):
    return make_user("adm", "adm@test.com", role="symotus_admin")


@pytest.fixture()
def alice(make_user):
    return make_user("alice", "alice@test.com", role="reseller")


@pytest.fixture()
def h(auth_headers, admin):
    return auth_headers(admin)


@pytest.fixture()
def plan_id(client, h):
    r = client.post("/billing/admin/plans", headers=h,
                    json={"name": "月約月繳", "term": "monthly", "cycle": "monthly", "price": 1500})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _create(client, h, alice, plan_id, start, cams=(44,), **kw):
    body = {"camera_ids": list(cams), "customer_id": alice.id, "plan_id": plan_id,
            "start_date": start.isoformat(), **kw}
    r = client.post("/billing/admin/subscriptions", headers=h, json=body)
    assert r.status_code == 200, r.text
    return r.json()["results"]


# ── 方案 ──

def test_月約年繳方案被拒(client, h):
    r = client.post("/billing/admin/plans", headers=h,
                    json={"name": "x", "term": "monthly", "cycle": "yearly", "price": 100})
    assert r.status_code == 422


def test_方案價格0合法_負數擋下(client, h):
    assert client.post("/billing/admin/plans", headers=h,
                       json={"name": "免費", "term": "annual", "cycle": "yearly", "price": 0}).status_code == 200
    assert client.post("/billing/admin/plans", headers=h,
                       json={"name": "x", "term": "annual", "cycle": "yearly", "price": -1}).status_code == 422


# ── 訂閱 ──

def test_建立訂閱當天開第一張帳單(client, h, alice, plan_id, db):
    res = _create(client, h, alice, plan_id, TODAY)
    assert res[0]["ok"] and res[0]["bills_created"] == 1
    sub = client.get(f"/billing/admin/subscriptions/{res[0]['subscription_id']}", headers=h).json()
    assert sub["camera_name"] == "cam44"
    assert sub["status"] == "active"
    assert sub["bills"][0]["state"] == "due"
    assert sub["bills"][0]["period_start"] == TODAY.isoformat()


def test_同一台相機不可重複訂閱(client, h, alice, plan_id):
    _create(client, h, alice, plan_id, TODAY)
    res = _create(client, h, alice, plan_id, TODAY, cams=(44, 45))
    assert res[0]["ok"] is False and "已有" in res[0]["error"]
    assert res[1]["ok"] is True


def test_付款人必須是reseller(client, h, admin, plan_id):
    r = client.post("/billing/admin/subscriptions", headers=h, json={
        "camera_ids": [44], "customer_id": admin.id, "plan_id": plan_id, "start_date": TODAY.isoformat()})
    assert r.status_code == 422


def test_起始日最多回溯92天(client, h, alice, plan_id):
    r = client.post("/billing/admin/subscriptions", headers=h, json={
        "camera_ids": [44], "customer_id": alice.id, "plan_id": plan_id,
        "start_date": (TODAY - timedelta(days=93)).isoformat()})
    assert r.status_code == 422


def test_回溯建立會補開並可標記已收款(client, h, alice, plan_id, db):
    res = _create(client, h, alice, plan_id, TODAY - timedelta(days=70), mark_past_due_paid=True)
    sid = res[0]["subscription_id"]
    bills = db.query(BillingBill).filter(BillingBill.subscription_id == sid).all()
    assert len(bills) >= 3
    for b in bills:
        assert (b.status == "paid") == (b.due_date < TODAY)


def test_未來起始日是預定狀態(client, h, alice, plan_id):
    res = _create(client, h, alice, plan_id, TODAY + timedelta(days=5))
    assert res[0]["bills_created"] == 0
    sub = client.get(f"/billing/admin/subscriptions/{res[0]['subscription_id']}", headers=h).json()
    assert sub["status"] == "scheduled"


def test_暫停恢復結束與鎖定(client, h, alice, plan_id, db):
    sid = _create(client, h, alice, plan_id, TODAY)[0]["subscription_id"]
    r = client.post(f"/billing/admin/subscriptions/{sid}/suspend", headers=h)
    assert r.status_code == 200 and r.json()["locked"] == "suspended"
    assert get_lock_map(db)[44]["state"] == "suspended"
    assert get_lock_map(db)[44]["serial"] == "SER44"
    assert client.post(f"/billing/admin/subscriptions/{sid}/suspend", headers=h).status_code == 409
    r = client.post(f"/billing/admin/subscriptions/{sid}/resume", headers=h)
    assert r.json()["status"] == "active"
    assert 44 not in get_lock_map(db)
    r = client.post(f"/billing/admin/subscriptions/{sid}/end", headers=h, json={"reason": "terminated"})
    assert r.json()["status"] == "ended" and r.json()["locked"] == "ended"
    # ended 是終態
    for act in ("suspend", "resume"):
        assert client.post(f"/billing/admin/subscriptions/{sid}/{act}", headers=h).status_code == 409
    r = client.post(f"/billing/admin/subscriptions/{sid}/release-lock", headers=h)
    assert r.json()["locked"] is None
    assert 44 not in get_lock_map(db)


def test_結束時處置未繳帳單(client, h, alice, plan_id, db):
    sid = _create(client, h, alice, plan_id, TODAY - timedelta(days=40))[0]["subscription_id"]
    bills = db.query(BillingBill).filter(BillingBill.subscription_id == sid).order_by(BillingBill.id).all()
    acts = {str(bills[0].id): "void", str(bills[1].id): "keep"}
    r = client.post(f"/billing/admin/subscriptions/{sid}/end", headers=h,
                    json={"reason": "non_payment", "bill_actions": acts})
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(BillingBill, bills[0].id).status == "void"
    assert db.get(BillingBill, bills[1].id).status == "unpaid"


def test_到期不續約(client, h, alice, plan_id):
    sid = _create(client, h, alice, plan_id, TODAY)[0]["subscription_id"]
    r = client.post(f"/billing/admin/subscriptions/{sid}/schedule-cancel", headers=h, json={"cancel": True})
    assert r.json()["cancel_at"] is not None
    assert r.json()["next_bill_date"] is None   # 下一個帳單日就是結束日，不再出帳
    r = client.post(f"/billing/admin/subscriptions/{sid}/schedule-cancel", headers=h, json={"cancel": False})
    assert r.json()["cancel_at"] is None


# ── 帳單 ──

def _overdue_sub(client, h, alice, plan_id, db, cam=44):
    sid = _create(client, h, alice, plan_id, TODAY - timedelta(days=40), cams=(cam,))[0]["subscription_id"]
    bills = db.query(BillingBill).filter(BillingBill.subscription_id == sid).order_by(BillingBill.id).all()
    return sid, bills


def test_直接標記收款並恢復服務(client, h, alice, plan_id, db):
    sid, bills = _overdue_sub(client, h, alice, plan_id, db)
    client.post(f"/billing/admin/subscriptions/{sid}/suspend", headers=h)
    overdue_ids = [b.id for b in bills if b.due_date < TODAY]
    r = client.post("/billing/admin/bills/mark-paid", headers=h, json={
        "bill_ids": overdue_ids, "paid_on": TODAY.isoformat(), "resume_subscriptions": True})
    assert r.status_code == 200, r.text
    assert r.json()["resumed"] == [sid]
    invalidate_lock_cache()
    assert 44 not in get_lock_map(db)


def test_還有逾期就不恢復(client, h, alice, plan_id, db):
    sid, bills = _overdue_sub(client, h, alice, plan_id, db)
    client.post(f"/billing/admin/subscriptions/{sid}/suspend", headers=h)
    r = client.post("/billing/admin/bills/mark-paid", headers=h, json={
        "bill_ids": [bills[1].id], "paid_on": TODAY.isoformat(), "resume_subscriptions": True})
    assert r.json()["resumed"] == []


def test_付款日不可晚於今天(client, h, alice, plan_id, db):
    _, bills = _overdue_sub(client, h, alice, plan_id, db)
    r = client.post("/billing/admin/bills/mark-paid", headers=h, json={
        "bill_ids": [bills[0].id], "paid_on": (TODAY + timedelta(days=1)).isoformat()})
    assert r.status_code == 422


def test_已收款不可作廢_作廢不可重開(client, h, alice, plan_id, db):
    _, bills = _overdue_sub(client, h, alice, plan_id, db)
    client.post("/billing/admin/bills/mark-paid", headers=h,
                json={"bill_ids": [bills[0].id], "paid_on": TODAY.isoformat()})
    assert client.post(f"/billing/admin/bills/{bills[0].id}/void", headers=h,
                       json={"reason": "x"}).status_code == 409
    assert client.post(f"/billing/admin/bills/{bills[1].id}/void", headers=h,
                       json={"reason": "折讓"}).status_code == 200
    # 排程再跑也不會補回作廢的期別
    from services.billing_v2 import advance_subscription
    sub = db.get(BillingSubscriptionV2, bills[1].subscription_id)
    db.expire_all()
    assert advance_subscription(db, sub, TODAY) == 0


def test_改金額(client, h, alice, plan_id, db):
    _, bills = _overdue_sub(client, h, alice, plan_id, db)
    r = client.put(f"/billing/admin/bills/{bills[0].id}/amount", headers=h, json={"amount": 1000, "reason": "議價"})
    assert r.json()["amount"] == 1000
    assert client.put(f"/billing/admin/bills/{bills[0].id}/amount", headers=h,
                      json={"amount": 1000, "reason": ""}).status_code == 422


# ── 逾期處置 ──

def test_逾期清單與套用(client, h, alice, plan_id, db):
    sid1, _ = _overdue_sub(client, h, alice, plan_id, db, cam=44)
    sid2, _ = _overdue_sub(client, h, alice, plan_id, db, cam=45)
    o = client.get("/billing/admin/overdue", headers=h).json()
    assert o["undecided"] == 2
    assert o["customers"][0]["customer_name"] == "alice"
    r = client.post("/billing/admin/overdue/apply", headers=h, json={"actions": [
        {"subscription_id": sid1, "action": "suspend"},
        {"subscription_id": sid2, "action": "end"},
        {"subscription_id": 9999, "action": "suspend"},
    ]}).json()["results"]
    assert [x["ok"] for x in r] == [True, True, False]   # 部分失敗要逐筆回報
    o = client.get("/billing/admin/overdue", headers=h).json()
    assert o["undecided"] == 0
    statuses = {s["subscription_id"]: s["status"] for s in o["customers"][0]["subscriptions"]}
    assert statuses == {sid1: "suspended", sid2: "ended"}   # 結束但保留應收，仍列在常駐頁


def test_dashboard(client, h, alice, plan_id, db):
    _overdue_sub(client, h, alice, plan_id, db)
    d = client.get("/billing/admin/dashboard", headers=h).json()
    assert d["overdue_count"] >= 1 and d["overdue_undecided"] == 1
    assert d["active_subscriptions"] == 1


def test_設定(client, h):
    r = client.put("/billing/admin/settings", headers=h, json={"payment_instructions": "轉帳至 000-1234"})
    assert r.json()["payment_instructions"] == "轉帳至 000-1234"
    assert client.get("/billing/admin/settings", headers=h).json()["payment_instructions"] == "轉帳至 000-1234"


def test_未納管相機清單(client, h, alice, plan_id):
    _create(client, h, alice, plan_id, TODAY)
    ids = [c["camera_id"] for c in client.get("/billing/admin/unsubscribed-cameras", headers=h).json()]
    assert ids == [45, 46]


# ── deny path ──

@pytest.mark.parametrize("method,path", [
    ("get", "/billing/admin/plans"), ("get", "/billing/admin/subscriptions"), ("get", "/billing/admin/bills"),
    ("get", "/billing/admin/overdue"), ("get", "/billing/admin/dashboard"),
    ("get", "/billing/admin/payment-reports"), ("post", "/billing/admin/subscriptions/1/suspend"),
    ("post", "/billing/admin/bills/mark-paid"),
])
def test_reseller打admin端點403(client, auth_headers, alice, method, path):
    r = getattr(client, method)(path, headers=auth_headers(alice))
    assert r.status_code == 403


def test_my端點只看自己(client, h, alice, plan_id, make_user, auth_headers):
    bob = make_user("bob", "bob@test.com", role="reseller")
    _create(client, h, alice, plan_id, TODAY - timedelta(days=40))
    assert len(client.get("/billing/my/bills", headers=auth_headers(alice)).json()) >= 2
    assert client.get("/billing/my/bills", headers=auth_headers(bob)).json() == []
    assert client.get("/billing/my/subscriptions", headers=auth_headers(bob)).json() == []
    n = client.get("/billing/notices/my", headers=auth_headers(alice)).json()
    assert n["overdue_count"] >= 1
    assert client.get("/billing/notices/my", headers=auth_headers(bob)).json()["overdue_count"] == 0


def test_notices_非reseller一律空(client, h, alice, plan_id, auth_headers, admin):
    _create(client, h, alice, plan_id, TODAY - timedelta(days=40))
    assert client.get("/billing/notices/my", headers=h).json()["overdue"] == []
