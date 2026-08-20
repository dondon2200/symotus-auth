"""客戶條件 PUT /billing/admin/customers/{user_id} 的邊界測試。

涵蓋兩個 P2 review 抓到的問題：
1. NOT NULL 欄位（billing_day/payment_method/statement_day）被明確傳 null 時，
   舊版會直接 setattr 成 None，違反 DB 的 NOT NULL 約束——要在 API 層擋成 422。
2. commission_type 決定該讀 commission_percent_bps 還是 commission_fixed_amount，
   兩者拆成獨立欄位後仍必須配對——型別與對應數值要一起帶，不能單位錯亂。
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


def test_commission設定後傳type為null可以清空(client, admin, reseller, auth_headers):
    h = auth_headers(admin)
    client.put(f"/billing/admin/customers/{reseller.id}", headers=h,
               json={"commission_type": "percent", "commission_percent_bps": 1000})
    r = client.put(f"/billing/admin/customers/{reseller.id}", headers=h,
                   json={"commission_type": None})
    assert r.status_code == 200
    assert r.json()["commission_type"] is None
    # 型別清除後，即使底層 commission_percent_bps 欄位還留著舊值也沒關係——
    # commission_amount/commission_display 只讀 commission_type 對應的欄位，
    # type 已是 None 時不會被讀到，顯示字串自然回空字串。
    assert r.json()["commission_display"] == ""


def test_只傳commission_type回422(client, admin, reseller, auth_headers):
    h = auth_headers(admin)
    r = client.put(f"/billing/admin/customers/{reseller.id}", headers=h, json={"commission_type": "percent"})
    assert r.status_code == 422


def test_只傳commission_percent_bps但客戶目前無型別回422(client, admin, reseller, auth_headers):
    h = auth_headers(admin)
    r = client.put(f"/billing/admin/customers/{reseller.id}", headers=h, json={"commission_percent_bps": 1000})
    assert r.status_code == 422


def test_commission雙欄位一起傳有效值成功(client, admin, reseller, auth_headers):
    h = auth_headers(admin)
    r = client.put(f"/billing/admin/customers/{reseller.id}", headers=h,
                   json={"commission_type": "percent", "commission_percent_bps": 1500})
    assert r.status_code == 200
    assert r.json()["commission_type"] == "percent"
    assert r.json()["commission_percent_bps"] == 1500


def test_type有值但對應數值為null回422(client, admin, reseller, auth_headers):
    h = auth_headers(admin)
    r = client.put(f"/billing/admin/customers/{reseller.id}", headers=h,
                   json={"commission_type": "percent", "commission_percent_bps": None})
    assert r.status_code == 422


def test_固定分潤可以超過一萬元(client, admin, reseller, auth_headers):
    # 這是拆欄位要解除的限制
    r = client.put(f"/billing/admin/customers/{reseller.id}", headers=auth_headers(admin),
                   json={"commission_type": "fixed", "commission_fixed_amount": 50000})
    assert r.status_code == 200
    assert r.json()["commission_fixed_amount"] == 50000
    assert r.json()["commission_display"] == "NT$ 50,000"


def test_百分比仍不得超過百分之百(client, admin, reseller, auth_headers):
    r = client.put(f"/billing/admin/customers/{reseller.id}", headers=auth_headers(admin),
                   json={"commission_type": "percent", "commission_percent_bps": 10001})
    assert r.status_code == 422


def test_設型別但缺對應數值回422(client, admin, reseller, auth_headers):
    h = auth_headers(admin)
    assert client.put(f"/billing/admin/customers/{reseller.id}", headers=h,
                      json={"commission_type": "percent"}).status_code == 422
    assert client.put(f"/billing/admin/customers/{reseller.id}", headers=h,
                      json={"commission_type": "fixed"}).status_code == 422


def test_型別與數值對不上回422(client, admin, reseller, auth_headers):
    h = auth_headers(admin)
    # percent 型別配 fixed 金額
    assert client.put(f"/billing/admin/customers/{reseller.id}", headers=h,
                      json={"commission_type": "percent", "commission_fixed_amount": 500}).status_code == 422
    # 已設為 percent 後，只送 fixed 金額（沒帶 type）也要擋
    client.put(f"/billing/admin/customers/{reseller.id}", headers=h,
               json={"commission_type": "percent", "commission_percent_bps": 1500})
    assert client.put(f"/billing/admin/customers/{reseller.id}", headers=h,
                      json={"commission_fixed_amount": 500}).status_code == 422


def test_清除分潤設定(client, admin, reseller, auth_headers):
    h = auth_headers(admin)
    client.put(f"/billing/admin/customers/{reseller.id}", headers=h,
               json={"commission_type": "percent", "commission_percent_bps": 1500})
    r = client.put(f"/billing/admin/customers/{reseller.id}", headers=h,
                   json={"commission_type": None})
    assert r.status_code == 200
    assert r.json()["commission_type"] is None
    assert r.json()["commission_display"] == ""
