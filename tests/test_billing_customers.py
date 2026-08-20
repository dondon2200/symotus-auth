"""客戶條件 PUT /billing/admin/customers/{user_id} 的邊界測試。

涵蓋兩個 P2 review 抓到的問題：
1. NOT NULL 欄位（billing_day/payment_method/statement_day）被明確傳 null 時，
   舊版會直接 setattr 成 None，違反 DB 的 NOT NULL 約束——要在 API 層擋成 422。
2. commission_type 與 commission_value 的單位互相依賴（type 決定 value 該讀成
   百分比 bps 還是 TWD 金額），只改其中一個會讓資料的單位語意不完整——要求兩者
   一起帶或都不帶。
"""
import pytest
from fastapi import FastAPI

from routers.billing import router as billing_router

PLAN = {"name": "標準方案", "monthly_fee": 1000, "timelapse_quota_secs": 0, "storage_quota_gb": 0}


@pytest.fixture()
def app():
    a = FastAPI()
    a.include_router(billing_router)
    return a


@pytest.fixture()
def admin(make_user):
    return make_user("cust_admin", "cust_admin@test.com", role="symotus_admin")


@pytest.fixture()
def reseller(make_user):
    return make_user("cust_reseller", "cust_reseller@test.com", role="reseller")


@pytest.mark.parametrize("field", ["billing_day", "payment_method", "statement_day"])
def test_not_null欄位傳null回422(client, admin, reseller, auth_headers, field):
    h = auth_headers(admin)
    r = client.put(f"/billing/admin/customers/{reseller.id}", headers=h, json={field: None})
    assert r.status_code == 422


def test_note傳null可以清空(client, admin, reseller, auth_headers):
    h = auth_headers(admin)
    client.put(f"/billing/admin/customers/{reseller.id}", headers=h, json={"note": "備註"})
    r = client.put(f"/billing/admin/customers/{reseller.id}", headers=h, json={"note": None})
    assert r.status_code == 200
    assert r.json()["note"] is None


def test_custom_monthly_fee傳null可以清空(client, admin, reseller, auth_headers):
    h = auth_headers(admin)
    client.put(f"/billing/admin/customers/{reseller.id}", headers=h, json={"custom_monthly_fee": 500})
    r = client.put(f"/billing/admin/customers/{reseller.id}", headers=h, json={"custom_monthly_fee": None})
    assert r.status_code == 200
    assert r.json()["custom_monthly_fee"] is None


def test_commission雙欄位一起傳null可以清空(client, admin, reseller, auth_headers):
    h = auth_headers(admin)
    client.put(f"/billing/admin/customers/{reseller.id}", headers=h,
               json={"commission_type": "percent", "commission_value": 1000})
    r = client.put(f"/billing/admin/customers/{reseller.id}", headers=h,
                   json={"commission_type": None, "commission_value": None})
    assert r.status_code == 200
    assert r.json()["commission_type"] is None
    assert r.json()["commission_value"] is None


def test_只傳commission_type回422(client, admin, reseller, auth_headers):
    h = auth_headers(admin)
    r = client.put(f"/billing/admin/customers/{reseller.id}", headers=h, json={"commission_type": "percent"})
    assert r.status_code == 422


def test_只傳commission_value回422(client, admin, reseller, auth_headers):
    h = auth_headers(admin)
    r = client.put(f"/billing/admin/customers/{reseller.id}", headers=h, json={"commission_value": 1000})
    assert r.status_code == 422


def test_commission雙欄位一起傳有效值成功(client, admin, reseller, auth_headers):
    h = auth_headers(admin)
    r = client.put(f"/billing/admin/customers/{reseller.id}", headers=h,
                   json={"commission_type": "percent", "commission_value": 1500})
    assert r.status_code == 200
    assert r.json()["commission_type"] == "percent"
    assert r.json()["commission_value"] == 1500


def test_type有值但value為null回422(client, admin, reseller, auth_headers):
    h = auth_headers(admin)
    r = client.put(f"/billing/admin/customers/{reseller.id}", headers=h,
                   json={"commission_type": "percent", "commission_value": None})
    assert r.status_code == 422
