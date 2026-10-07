"""付款回報與確認（spec §6.5、§13 S14–S18）。"""
from datetime import timedelta

import pytest
from fastapi import FastAPI

import routers.billing as billing_mod
from models import BillingBill
from routers.billing import router as billing_router
from services.billing_dates import taipei_today

TODAY = taipei_today()
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture()
def app():
    a = FastAPI()
    a.include_router(billing_router)
    return a


@pytest.fixture(autouse=True)
def _no_camera_backend(monkeypatch):
    async def fake_resolve(cid):
        return f"cam{cid}", f"SER{cid}"
    monkeypatch.setattr(billing_mod, "_resolve_camera", fake_resolve)


@pytest.fixture()
def admin(make_user):
    return make_user("adm", "adm@test.com", role="symotus_admin")


@pytest.fixture()
def alice(make_user):
    return make_user("alice", "alice@test.com", role="reseller")


@pytest.fixture()
def bob(make_user):
    return make_user("bob", "bob@test.com", role="reseller")


@pytest.fixture()
def h(auth_headers, admin):
    return auth_headers(admin)


@pytest.fixture()
def ha(auth_headers, alice):
    return auth_headers(alice)


@pytest.fixture()
def bills(client, h, alice, db):
    """alice 有一份 40 天前開始的月約，兩張逾期帳單＋一張待繳。"""
    pid = client.post("/billing/admin/plans", headers=h,
                      json={"name": "月", "term": "monthly", "cycle": "monthly", "price": 1500}).json()["id"]
    r = client.post("/billing/admin/subscriptions", headers=h, json={
        "camera_ids": [44], "customer_id": alice.id, "plan_id": pid,
        "start_date": (TODAY - timedelta(days=40)).isoformat()})
    sid = r.json()["results"][0]["subscription_id"]
    return db.query(BillingBill).filter(BillingBill.subscription_id == sid).order_by(BillingBill.id).all()


def _report(client, headers, bill_ids, amount=3000, method="transfer", last5="12345", receipt=None, **kw):
    data = {"bill_ids": ",".join(str(i) for i in bill_ids), "paid_on": kw.get("paid_on", TODAY.isoformat()),
            "amount": str(amount), "method": method}
    if last5 is not None:
        data["account_last5"] = last5
    files = {"receipt": ("r.png", receipt, "image/png")} if receipt else None
    return client.post("/billing/my/payment-reports", headers=headers, data=data, files=files)


def test_回報後帳單是確認中_逾期照算(client, ha, bills, db):
    r = _report(client, ha, [bills[0].id, bills[1].id], receipt=PNG)
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["status"] == "pending" and rep["difference"] == 0 and rep["has_receipt"]
    my = {b["id"]: b for b in client.get("/billing/my/bills", headers=ha).json()}
    assert my[bills[0].id]["state"] == "pending"
    assert my[bills[0].id]["status"] == "unpaid"          # 回報不改帳單狀態
    assert my[bills[0].id]["overdue"] is True             # 逾期照算
    n = client.get("/billing/notices/my", headers=ha).json()
    assert {b["id"] for b in n["pending"]} == {bills[0].id, bills[1].id}
    assert all(b["id"] not in {bills[0].id, bills[1].id} for b in n["overdue"])


def test_確認_部分確認_付款日用回報日(client, h, ha, bills, db):
    rid = _report(client, ha, [bills[0].id, bills[1].id], amount=1500,
                  paid_on=(TODAY - timedelta(days=2)).isoformat()).json()["id"]
    d = client.get(f"/billing/admin/payment-reports/{rid}", headers=h).json()
    assert d["difference"] == -1500
    r = client.post(f"/billing/admin/payment-reports/{rid}/confirm", headers=h,
                    json={"bill_ids": [bills[0].id]})
    assert r.status_code == 200, r.text
    db.expire_all()
    b0, b1 = db.get(BillingBill, bills[0].id), db.get(BillingBill, bills[1].id)
    assert b0.status == "paid" and b0.paid_on == TODAY - timedelta(days=2)
    assert b0.paid_via_report_id == rid and "末五碼 12345" in b0.paid_note
    assert b1.status == "unpaid" and b1.pending_report_id is None   # 沒勾的回到未繳、可再回報
    confirmed = {b["id"]: b["confirmed"] for b in r.json()["bills"]}
    assert confirmed == {bills[0].id: True, bills[1].id: False}
    assert _report(client, ha, [bills[1].id], amount=1500).status_code == 200


def test_確認後恢復服務(client, h, ha, bills, db):
    sid = bills[0].subscription_id
    client.post(f"/billing/admin/subscriptions/{sid}/suspend", headers=h)
    overdue = [b.id for b in bills if b.due_date < TODAY]
    rid = _report(client, ha, overdue, amount=1500 * len(overdue)).json()["id"]
    d = client.get(f"/billing/admin/payment-reports/{rid}", headers=h).json()
    assert d["resumable_subscription_ids"] == [sid]
    r = client.post(f"/billing/admin/payment-reports/{rid}/confirm", headers=h,
                    json={"bill_ids": overdue, "resume_subscriptions": True})
    assert r.json()["resumed"] == [sid]


def test_退回必填原因_reseller看得到(client, h, ha, bills):
    rid = _report(client, ha, [bills[0].id], amount=1500).json()["id"]
    assert client.post(f"/billing/admin/payment-reports/{rid}/reject", headers=h,
                       json={"reason": "  "}).status_code == 422
    r = client.post(f"/billing/admin/payment-reports/{rid}/reject", headers=h, json={"reason": "查無此筆入帳"})
    assert r.json()["status"] == "rejected"
    n = client.get("/billing/notices/my", headers=ha).json()
    assert n["rejected"][0]["reason"] == "查無此筆入帳"
    assert _report(client, ha, [bills[0].id], amount=1500).status_code == 200   # 可重新回報


def test_撤回(client, ha, bills):
    rid = _report(client, ha, [bills[0].id], amount=1500).json()["id"]
    assert client.post(f"/billing/my/payment-reports/{rid}/withdraw", headers=ha).json()["status"] == "withdrawn"
    assert client.post(f"/billing/my/payment-reports/{rid}/withdraw", headers=ha).status_code == 409
    assert _report(client, ha, [bills[0].id], amount=1500).status_code == 200


def test_同一張帳單不可在兩份pending(client, ha, bills):
    assert _report(client, ha, [bills[0].id], amount=1500).status_code == 200
    r = _report(client, ha, [bills[0].id, bills[1].id])
    assert r.status_code == 409 and r.json()["detail"]["bill_ids"] == [bills[0].id]


def test_pending中直接標記收款_作廢_改金額都409(client, h, ha, bills):
    _report(client, ha, [bills[0].id], amount=1500)
    bid = bills[0].id
    r = client.post("/billing/admin/bills/mark-paid", headers=h,
                    json={"bill_ids": [bid], "paid_on": TODAY.isoformat()})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "BILL_IN_PENDING_REPORT"
    assert client.post(f"/billing/admin/bills/{bid}/void", headers=h, json={"reason": "x"}).status_code == 409
    assert client.put(f"/billing/admin/bills/{bid}/amount", headers=h,
                      json={"amount": 1, "reason": "x"}).status_code == 409


def test_結束訂閱時pending帳單不可作廢(client, h, ha, bills):
    _report(client, ha, [bills[0].id], amount=1500)
    r = client.post(f"/billing/admin/subscriptions/{bills[0].subscription_id}/end", headers=h,
                    json={"reason": "non_payment", "bill_actions": {str(bills[0].id): "void"}})
    assert r.status_code == 409


def test_已收款的帳單不可回報(client, h, ha, bills):
    client.post("/billing/admin/bills/mark-paid", headers=h,
                json={"bill_ids": [bills[0].id], "paid_on": TODAY.isoformat()})
    r = _report(client, ha, [bills[0].id], amount=1500)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "BILL_NOT_UNPAID"


def test_他人帳單404(client, auth_headers, bob, bills):
    assert _report(client, auth_headers(bob), [bills[0].id], amount=1500).status_code == 404


def test_他人收據404(client, ha, auth_headers, bob, bills):
    rid = _report(client, ha, [bills[0].id], amount=1500, receipt=PNG).json()["id"]
    assert client.get(f"/billing/my/payment-reports/{rid}/receipt", headers=ha).status_code == 200
    assert client.get(f"/billing/my/payment-reports/{rid}/receipt", headers=auth_headers(bob)).status_code == 404


def test_收據下載標頭(client, h, ha, bills):
    rid = _report(client, ha, [bills[0].id], amount=1500, receipt=b"%PDF-1.4 xxx").json()["id"]
    r = client.get(f"/billing/admin/payment-reports/{rid}/receipt", headers=h)
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "attachment" in r.headers["content-disposition"]


@pytest.mark.parametrize("kw,code", [
    ({"paid_on": (TODAY + timedelta(days=1)).isoformat()}, 422),
    ({"last5": "1234"}, 422),
    ({"last5": "12a45"}, 422),
    ({"last5": None}, 422),                       # 轉帳缺末五碼
    ({"amount": 0}, 422),
    ({"receipt": b"<html>not an image</html>"}, 422),
    ({"receipt": b"\xff\xd8\xff" + b"0" * (2 * 1024 * 1024)}, 422),
])
def test_欄位驗證(client, ha, bills, kw, code):
    args = {"amount": 1500, **kw}
    assert _report(client, ha, [bills[0].id], **args).status_code == code


def test_現金不用末五碼(client, ha, bills):
    assert _report(client, ha, [bills[0].id], amount=1500, method="cash", last5=None).status_code == 200


def test_pending上限10份(client, h, alice, ha, db):
    pid = client.post("/billing/admin/plans", headers=h,
                      json={"name": "月", "term": "monthly", "cycle": "monthly", "price": 100}).json()["id"]
    client.post("/billing/admin/subscriptions", headers=h, json={
        "camera_ids": list(range(100, 111)), "customer_id": alice.id, "plan_id": pid,
        "start_date": TODAY.isoformat()})
    ids = [b.id for b in db.query(BillingBill).order_by(BillingBill.id).all()]
    for bid in ids[:10]:
        assert _report(client, ha, [bid], amount=100).status_code == 200
    assert _report(client, ha, [ids[10]], amount=100).status_code == 409


def test_只有reseller能回報(client, h, bills):
    assert _report(client, h, [bills[0].id], amount=1500).status_code == 403


def test_已處理的回報不可再處理(client, h, ha, bills):
    rid = _report(client, ha, [bills[0].id], amount=1500).json()["id"]
    client.post(f"/billing/admin/payment-reports/{rid}/confirm", headers=h, json={"bill_ids": [bills[0].id]})
    assert client.post(f"/billing/admin/payment-reports/{rid}/reject", headers=h,
                       json={"reason": "x"}).status_code == 409
    assert client.post(f"/billing/admin/payment-reports/{rid}/confirm", headers=h,
                       json={"bill_ids": [bills[0].id]}).status_code == 409


def test_只能確認回報裡的帳單(client, h, ha, bills):
    rid = _report(client, ha, [bills[0].id], amount=1500).json()["id"]
    assert client.post(f"/billing/admin/payment-reports/{rid}/confirm", headers=h,
                       json={"bill_ids": [bills[1].id]}).status_code == 422


def test_admin逾期清單標出已回報(client, h, ha, bills):
    overdue = [b.id for b in bills if b.due_date < TODAY]
    _report(client, ha, overdue, amount=1500 * len(overdue))
    row = client.get("/billing/admin/overdue", headers=h).json()["customers"][0]["subscriptions"][0]
    assert row["all_pending"] is True and row["reported_at"]
    pend = client.get("/billing/admin/payment-reports?status=pending", headers=h).json()
    assert len(pend) == 1 and pend[0]["waiting_days"] == 0
