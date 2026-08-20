import pytest
from fastapi import FastAPI

from routers.billing import router as billing_router

PLAN = {"name": "標準方案", "monthly_fee": 1200, "timelapse_quota_secs": 3600, "storage_quota_gb": 100}
PERIOD = "2026-08"


@pytest.fixture()
def app():
    a = FastAPI()
    a.include_router(billing_router)
    return a


@pytest.fixture()
def admin(make_user):
    return make_user("self_admin", "self_admin@test.com", role="symotus_admin")


@pytest.fixture()
def alice(make_user):
    return make_user("alice", "alice@test.com", role="reseller")


@pytest.fixture()
def bob(make_user):
    return make_user("bob", "bob@test.com", role="reseller")


@pytest.fixture()
def setup(client, admin, alice, bob, auth_headers):
    """alice 有一台相機的訂閱與一張發票；bob 什麼都沒有。"""
    h = auth_headers(admin)
    pid = client.post("/billing/admin/plans", json=PLAN, headers=h).json()["id"]
    client.post("/billing/admin/subscriptions",
                json={"camera_id": 7, "customer_id": alice.id, "plan_id": pid}, headers=h)
    client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=h)
    return h


def test_只看得到自己的訂閱(client, setup, alice, bob, auth_headers):
    mine = client.get("/billing/subscriptions/my", headers=auth_headers(alice)).json()
    assert len(mine) == 1
    assert mine[0]["camera_id"] == 7
    assert mine[0]["monthly_fee"] == 1200

    assert client.get("/billing/subscriptions/my", headers=auth_headers(bob)).json() == []


def test_只看得到自己的發票(client, setup, alice, bob, auth_headers):
    assert len(client.get("/billing/invoices/my", headers=auth_headers(alice)).json()) == 1
    assert client.get("/billing/invoices/my", headers=auth_headers(bob)).json() == []


def test_不能讀他人的發票明細(client, setup, alice, bob, auth_headers):
    inv_id = client.get("/billing/invoices/my", headers=auth_headers(alice)).json()[0]["id"]
    # bob 猜到 id 也讀不到——這是舊版 PDF 連結的漏洞型態
    assert client.get(f"/billing/invoices/{inv_id}", headers=auth_headers(bob)).status_code == 404
    assert client.get(f"/billing/invoices/{inv_id}", headers=auth_headers(alice)).status_code == 200


def test_invoices_my不會被id路由吃掉(client, setup, alice, auth_headers):
    # 若 /{invoice_id} 註冊在 /my 之前，這裡會得到 422
    assert client.get("/billing/invoices/my", headers=auth_headers(alice)).status_code == 200


def test_配額回方案上限且用量預設為零(client, setup, alice, auth_headers):
    q = client.get("/billing/quotas/my", headers=auth_headers(alice)).json()
    assert len(q) == 1
    assert q[0]["camera_id"] == 7
    assert q[0]["timelapse_total_secs"] == 3600
    assert q[0]["storage_total_gb"] == 100
    assert q[0]["timelapse_used_secs"] == 0     # 用量採集在階段三才上線
    assert q[0]["state"] == "ok"


def test_未登入不能讀自助端點(client, setup):
    assert client.get("/billing/subscriptions/my").status_code in (401, 403)
