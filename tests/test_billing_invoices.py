import pytest
from fastapi import FastAPI

from routers.billing import router as billing_router

PLAN_A = {"name": "標準方案", "monthly_fee": 1200, "timelapse_quota_secs": 3600, "storage_quota_gb": 100}
PLAN_B = {"name": "進階方案", "monthly_fee": 800, "timelapse_quota_secs": 7200, "storage_quota_gb": 200}
PERIOD = "2026-08"


@pytest.fixture()
def app():
    a = FastAPI()
    a.include_router(billing_router)
    return a


@pytest.fixture()
def admin(make_user):
    return make_user("inv_admin", "inv_admin@test.com", role="symotus_admin")


@pytest.fixture()
def reseller(make_user):
    return make_user("inv_reseller", "inv_reseller@test.com", role="reseller")


@pytest.fixture()
def two_subs(client, admin, reseller, auth_headers):
    """該客戶有兩台相機、兩個方案，月費合計 2000。"""
    h = auth_headers(admin)
    a = client.post("/billing/admin/plans", json=PLAN_A, headers=h).json()["id"]
    b = client.post("/billing/admin/plans", json=PLAN_B, headers=h).json()["id"]
    client.post("/billing/admin/subscriptions",
                json={"camera_id": 1, "customer_id": reseller.id, "plan_id": a}, headers=h)
    client.post("/billing/admin/subscriptions",
                json={"camera_id": 2, "customer_id": reseller.id, "plan_id": b}, headers=h)
    return h


def test_產生發票金額為訂閱月費加總(client, two_subs, reseller):
    r = client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=two_subs)
    assert r.status_code == 200
    invoices = client.get(f"/billing/admin/invoices?period={PERIOD}", headers=two_subs).json()
    assert len(invoices) == 1
    assert invoices[0]["total"] == 2000
    assert invoices[0]["status"] == "unpaid"


def test_重複產生不會產生第二張發票(client, two_subs):
    client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=two_subs)
    client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=two_subs)
    client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=two_subs)
    invoices = client.get(f"/billing/admin/invoices?period={PERIOD}", headers=two_subs).json()
    assert len(invoices) == 1


def test_發票明細一台相機一行且存快照(client, two_subs):
    client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=two_subs)
    inv = client.get(f"/billing/admin/invoices?period={PERIOD}", headers=two_subs).json()[0]
    detail = client.get(f"/billing/admin/invoices/{inv['id']}", headers=two_subs).json()
    assert len(detail["lines"]) == 2
    assert sorted(l["amount"] for l in detail["lines"]) == [800, 1200]
    assert sorted(l["plan_name"] for l in detail["lines"]) == ["標準方案", "進階方案"]


def test_方案改價不影響已開立的發票(client, two_subs):
    client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=two_subs)
    plans = client.get("/billing/admin/plans", headers=two_subs).json()
    target = [p for p in plans if p["name"] == "標準方案"][0]
    client.put(f"/billing/admin/plans/{target['id']}",
               json={**PLAN_A, "monthly_fee": 9999}, headers=two_subs)

    inv = client.get(f"/billing/admin/invoices?period={PERIOD}", headers=two_subs).json()[0]
    assert inv["total"] == 2000  # 仍是開立當時的金額


def test_取消的訂閱不列入新發票(client, two_subs, reseller):
    subs = client.get("/billing/admin/subscriptions", headers=two_subs).json()
    client.delete(f"/billing/admin/subscriptions/{subs[0]['id']}", headers=two_subs)
    client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=two_subs)
    inv = client.get(f"/billing/admin/invoices?period={PERIOD}", headers=two_subs).json()[0]
    assert inv["total"] == 800


def test_沒有訂閱的客戶不產生發票(client, admin, make_user, auth_headers):
    make_user("no_sub", "no_sub@test.com", role="reseller")
    h = auth_headers(admin)
    client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=h)
    assert client.get(f"/billing/admin/invoices?period={PERIOD}", headers=h).json() == []


def test_標記收款(client, two_subs):
    client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=two_subs)
    inv = client.get(f"/billing/admin/invoices?period={PERIOD}", headers=two_subs).json()[0]
    r = client.post(f"/billing/admin/invoices/{inv['id']}/mark-paid", headers=two_subs)
    assert r.status_code == 200
    after = client.get(f"/billing/admin/invoices?period={PERIOD}", headers=two_subs).json()[0]
    assert after["status"] == "paid"
    assert after["paid_at"] is not None


def test_已作廢的發票不能標記收款(client, two_subs):
    client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=two_subs)
    inv = client.get(f"/billing/admin/invoices?period={PERIOD}", headers=two_subs).json()[0]
    client.post(f"/billing/admin/invoices/{inv['id']}/void", headers=two_subs)
    r = client.post(f"/billing/admin/invoices/{inv['id']}/mark-paid", headers=two_subs)
    assert r.status_code == 409


def test_作廢後重新產生會開新發票且舊發票留在歷史中(client, two_subs):
    client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=two_subs)
    first = client.get(f"/billing/admin/invoices?period={PERIOD}", headers=two_subs).json()[0]
    client.post(f"/billing/admin/invoices/{first['id']}/void", headers=two_subs)

    r = client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=two_subs)
    assert r.status_code == 200
    assert r.json()["created"] == 1

    invoices = client.get(f"/billing/admin/invoices?period={PERIOD}", headers=two_subs).json()
    assert len(invoices) == 2
    by_status = {i["status"]: i for i in invoices}
    assert by_status["void"]["id"] == first["id"]
    assert by_status["unpaid"]["total"] == 2000


def test_已收款的發票不能直接作廢(client, two_subs):
    client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=two_subs)
    inv = client.get(f"/billing/admin/invoices?period={PERIOD}", headers=two_subs).json()[0]
    client.post(f"/billing/admin/invoices/{inv['id']}/mark-paid", headers=two_subs)
    r = client.post(f"/billing/admin/invoices/{inv['id']}/void", headers=two_subs)
    assert r.status_code == 409

    after = client.get(f"/billing/admin/invoices?period={PERIOD}", headers=two_subs).json()[0]
    assert after["status"] == "paid"
    assert after["paid_at"] is not None


def test_期別格式錯誤回422(client, two_subs):
    assert client.post("/billing/admin/invoices/generate/2026-8", headers=two_subs).status_code == 422
    assert client.post("/billing/admin/invoices/generate/abc", headers=two_subs).status_code == 422


def test_reseller不能產生發票(client, reseller, auth_headers):
    r = client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=auth_headers(reseller))
    assert r.status_code == 403


def test_總覽統計(client, two_subs):
    client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=two_subs)
    d = client.get(f"/billing/admin/dashboard?period={PERIOD}", headers=two_subs).json()
    assert d["total_billed"] == 2000
    assert d["total_unpaid"] == 2000
    assert d["invoice_count"] == 1
    assert d["frozen_count"] == 0


def test_已存在非void發票時併發產生仍回成功語意(client, two_subs, monkeypatch):
    """模擬部分唯一索引擋下重複列的真實併發情境：
    commit 拋出 IntegrityError，但重查後發現該期別確實已有非 void 發票，
    此時應維持「已由其他操作產生」的成功回應，而不是往上拋錯。
    """
    from sqlalchemy.exc import IntegrityError
    import routers.billing as billing_module

    client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=two_subs)

    real_commit = billing_module.Session.commit
    call_count = {"n": 0}

    def fake_commit(self):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise IntegrityError("stmt", "params", Exception("dup key"))
        return real_commit(self)

    monkeypatch.setattr(billing_module.Session, "commit", fake_commit)
    r = client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=two_subs)
    assert r.status_code == 200
    assert r.json()["created"] == 0
    assert "已由其他操作產生" in r.json()["message"]


def test_作廢後重開dashboard張數不含void(client, two_subs):
    client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=two_subs)
    first = client.get(f"/billing/admin/invoices?period={PERIOD}", headers=two_subs).json()[0]
    client.post(f"/billing/admin/invoices/{first['id']}/void", headers=two_subs)
    client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=two_subs)

    invoices = client.get(f"/billing/admin/invoices?period={PERIOD}", headers=two_subs).json()
    assert len(invoices) == 2  # 一張 void、一張新開

    d = client.get(f"/billing/admin/dashboard?period={PERIOD}", headers=two_subs).json()
    assert d["invoice_count"] == 1
    new_inv = [i for i in invoices if i["status"] != "void"][0]
    assert d["total_billed"] == new_inv["total"]


def test_重複標記收款不改變付款時間(client, two_subs):
    client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=two_subs)
    inv = client.get(f"/billing/admin/invoices?period={PERIOD}", headers=two_subs).json()[0]
    r1 = client.post(f"/billing/admin/invoices/{inv['id']}/mark-paid", headers=two_subs)
    assert r1.status_code == 200
    first_paid_at = client.get(
        f"/billing/admin/invoices?period={PERIOD}", headers=two_subs
    ).json()[0]["paid_at"]

    r2 = client.post(f"/billing/admin/invoices/{inv['id']}/mark-paid", headers=two_subs)
    assert r2.status_code == 200
    second_paid_at = client.get(
        f"/billing/admin/invoices?period={PERIOD}", headers=two_subs
    ).json()[0]["paid_at"]

    assert first_paid_at == second_paid_at
