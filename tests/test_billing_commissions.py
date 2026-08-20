"""分潤應付報表。

設計決定：分潤不進發票（發票照定價開），另列「我要付給經銷商多少」。
所以這裡的測試除了算得對，也要確認發票金額**不受分潤影響**。
"""
import pytest
from fastapi import FastAPI

from routers.billing import router as billing_router

PLAN = {"name": "標準方案", "monthly_fee": 1000, "timelapse_quota_secs": 0, "storage_quota_gb": 0}
PERIOD = "2026-08"


@pytest.fixture()
def app():
    a = FastAPI()
    a.include_router(billing_router)
    return a


@pytest.fixture()
def admin(make_user):
    return make_user("comm_admin", "comm_admin@test.com", role="symotus_admin")


@pytest.fixture()
def reseller(make_user):
    return make_user("comm_reseller", "comm_reseller@test.com", role="reseller")


@pytest.fixture()
def billed(client, admin, reseller, auth_headers, db):
    """該經銷商本期有一張 1000 元的發票。"""
    from models import BillingSubscription
    from datetime import datetime, timedelta

    h = auth_headers(admin)
    pid = client.post("/billing/admin/plans", json=PLAN, headers=h).json()["id"]
    client.post("/billing/admin/subscriptions",
                json={"camera_id": 1, "customer_id": reseller.id, "plan_id": pid}, headers=h)
    # 訂閱必須在期別開始前就存在才計費（開通當月免費）
    sub = db.query(BillingSubscription).filter(BillingSubscription.camera_id == 1).first()
    sub.started_at = datetime(2026, 1, 1)
    db.commit()
    client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=h)
    return h


def test_百分比分潤(client, billed, reseller):
    client.put(f"/billing/admin/customers/{reseller.id}", headers=billed,
               json={"commission_type": "percent", "commission_value": 1500})
    rows = client.get(f"/billing/admin/commissions?period={PERIOD}", headers=billed).json()
    row = [r for r in rows if r["customer_id"] == reseller.id][0]
    assert row["invoice_total"] == 1000
    assert row["commission_amount"] == 150


def test_固定金額分潤(client, billed, reseller):
    client.put(f"/billing/admin/customers/{reseller.id}", headers=billed,
               json={"commission_type": "fixed", "commission_value": 300})
    rows = client.get(f"/billing/admin/commissions?period={PERIOD}", headers=billed).json()
    assert [r for r in rows if r["customer_id"] == reseller.id][0]["commission_amount"] == 300


def test_分潤不影響發票金額(client, billed, reseller):
    client.put(f"/billing/admin/customers/{reseller.id}", headers=billed,
               json={"commission_type": "percent", "commission_value": 1500})
    inv = client.get(f"/billing/admin/invoices?period={PERIOD}", headers=billed).json()[0]
    assert inv["total"] == 1000   # 照定價開，沒有被扣掉分潤


def test_未設分潤的客戶不出現在報表(client, billed, reseller):
    rows = client.get(f"/billing/admin/commissions?period={PERIOD}", headers=billed).json()
    assert [r for r in rows if r["customer_id"] == reseller.id] == []


def test_作廢的發票不計入分潤基數(client, billed, reseller):
    client.put(f"/billing/admin/customers/{reseller.id}", headers=billed,
               json={"commission_type": "percent", "commission_value": 1500})
    inv = client.get(f"/billing/admin/invoices?period={PERIOD}", headers=billed).json()[0]
    client.post(f"/billing/admin/invoices/{inv['id']}/void", headers=billed)

    rows = client.get(f"/billing/admin/commissions?period={PERIOD}", headers=billed).json()
    row = [r for r in rows if r["customer_id"] == reseller.id]
    assert row == [] or row[0]["commission_amount"] == 0


def test_期別格式錯誤回422(client, billed):
    assert client.get("/billing/admin/commissions?period=2026-8", headers=billed).status_code == 422


def test_reseller不能看分潤報表(client, reseller, auth_headers, billed):
    r = client.get(f"/billing/admin/commissions?period={PERIOD}", headers=auth_headers(reseller))
    assert r.status_code == 403


def test_小數分潤的報表金額(client, billed, reseller):
    # 1000 元的發票、12.5% 分潤 → 125 元
    client.put(f"/billing/admin/customers/{reseller.id}", headers=billed,
               json={"commission_type": "percent", "commission_value": 1250})
    rows = client.get(f"/billing/admin/commissions?period={PERIOD}", headers=billed).json()
    row = [r for r in rows if r["customer_id"] == reseller.id][0]
    assert row["commission_amount"] == 125
    assert row["commission_display"] == "12.5%"
