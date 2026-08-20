from datetime import datetime

import pytest
from fastapi import FastAPI

from routers.billing import router as billing_router
from models import BillingSubscription

PLAN_A = {"name": "標準方案", "monthly_fee": 1200, "timelapse_quota_secs": 3600, "storage_quota_gb": 100}
PLAN_B = {"name": "進階方案", "monthly_fee": 800, "timelapse_quota_secs": 7200, "storage_quota_gb": 200}
PERIOD = "2026-08"


def _backdate_all_subscriptions(db, before: datetime = datetime(2026, 7, 1)):
    """測試預設用『本月剛訂閱』無法涵蓋帳單邏輯（入會當月免費），
    這裡把訂閱的 started_at 往前搬，模擬『在該期別開始前就已訂閱』。"""
    for s in db.query(BillingSubscription).all():
        s.started_at = before
    db.commit()


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
def two_subs(client, admin, reseller, auth_headers, db):
    """該客戶有兩台相機、兩個方案，月費合計 2000。訂閱時間回填到期別開始之前，
    否則依「入會當月免費」規則這兩份訂閱在 PERIOD 當月不會被計費。"""
    h = auth_headers(admin)
    a = client.post("/billing/admin/plans", json=PLAN_A, headers=h).json()["id"]
    b = client.post("/billing/admin/plans", json=PLAN_B, headers=h).json()["id"]
    client.post("/billing/admin/subscriptions",
                json={"camera_id": 1, "customer_id": reseller.id, "plan_id": a}, headers=h)
    client.post("/billing/admin/subscriptions",
                json={"camera_id": 2, "customer_id": reseller.id, "plan_id": b}, headers=h)
    _backdate_all_subscriptions(db)
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


def test_期別開始前就已取消的訂閱不列入新發票(client, two_subs, reseller, db):
    """取消發生在期別開始「之前」，代表該訂閱在這個期別完全沒有生效過，
    自然不該被計費——這與『取消的訂閱仍可補開歷史期別發票』（見下一則測試）
    是兩回事：關鍵是取消時間點相對於期別起點的先後。"""
    subs = client.get("/billing/admin/subscriptions", headers=two_subs).json()
    client.delete(f"/billing/admin/subscriptions/{subs[0]['id']}", headers=two_subs)
    from models import BillingSubscription
    from datetime import datetime
    sub = db.query(BillingSubscription).filter(BillingSubscription.id == subs[0]["id"]).first()
    sub.cancelled_at = datetime(2026, 7, 15)  # 早於 PERIOD(2026-08) 的期別起點
    db.commit()

    client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=two_subs)
    inv = client.get(f"/billing/admin/invoices?period={PERIOD}", headers=two_subs).json()[0]
    assert inv["total"] == 800


def test_取消的訂閱仍可補開取消前的歷史期別發票(client, two_subs, reseller, db):
    """design §5：取消不是真刪，取消『之後』的期別才不計費；取消『之前』
    曾經生效過的期別，仍應能事後補開發票（例如上個月忘記跑產生作業）。"""
    from models import BillingSubscription, BillingInvoice
    from datetime import datetime

    subs = db.query(BillingSubscription).all()
    target = subs[0]
    target.cancelled_at = datetime(2026, 8, 15)  # 在 PERIOD(2026-08) 期間內取消
    target.status = "cancelled"
    db.commit()

    r = client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=two_subs)
    assert r.status_code == 200
    inv = db.query(BillingInvoice).filter(BillingInvoice.period == PERIOD).first()
    assert inv is not None
    assert inv.total == 2000  # 取消發生在期別「之內」，該期仍完整計費


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


def test_A已有發票B沒有時各自獨立產生且回報created與skipped(client, admin, reseller, make_user, auth_headers, db):
    """A 客戶這期已有發票、B 客戶沒有。舊版整批只 commit 一次，
    若批次中有客戶觸發部分唯一索引，rollback 會連同其他「這次呼叫本該建立」的
    客戶一起丟掉，卻仍回報成功。改成逐客戶 savepoint 後：
    A 應被跳過(skipped)、B 應正常建立(created)，且 B 的發票要「真的」寫進 DB，
    不能只是回應數字對但資料庫其實是空的——這正是舊版被靜默吃掉的情境。
    """
    from models import BillingInvoice as Invoice

    h = auth_headers(admin)
    plan_id = client.post("/billing/admin/plans", json=PLAN_A, headers=h).json()["id"]
    bob = make_user("inv_bob", "inv_bob@test.com", role="reseller")

    client.post("/billing/admin/subscriptions",
                json={"camera_id": 101, "customer_id": reseller.id, "plan_id": plan_id}, headers=h)
    client.post("/billing/admin/subscriptions",
                json={"camera_id": 102, "customer_id": bob.id, "plan_id": plan_id}, headers=h)
    _backdate_all_subscriptions(db)

    # A（reseller）直接手動先有一張這期的發票；B（bob）還沒有
    db.add(Invoice(customer_id=reseller.id, period=PERIOD, total=1200, status="unpaid"))
    db.commit()

    r = client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["created"] == 1
    assert body["skipped"] == 1

    assert db.query(Invoice).filter(
        Invoice.customer_id == bob.id, Invoice.period == PERIOD,
    ).count() == 1


def test_重跑同期別回報skipped_existing而非conflict(client, two_subs):
    """重新產生已經有發票的期別，屬於正常的冪等跳過，不是併發衝突——
    回應應該用 skipped_existing 計數，且訊息不該講『併發衝突』。"""
    client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=two_subs)
    r = client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=two_subs)
    body = r.json()
    assert body["created"] == 0
    assert body["skipped_existing"] == 1
    assert body["skipped_conflict"] == 0
    assert body["skipped"] == 1
    assert "併發衝突" not in body["message"]


def test_savepoint隔離_單一客戶flush失敗不影響其他客戶(client, admin, reseller, make_user, auth_headers, db, monkeypatch):
    """generate_invoices 對每個客戶各自開一個 begin_nested() savepoint。
    這裡直接讓第二個客戶（bob）在 flush 當下真的拋出 IntegrityError（而非走
    『existing 預查』就跳過的路徑），驗證：
    1) 第一個客戶（reseller）的發票確實寫進 DB、不會被第二個客戶的失敗牽連 rollback 掉
    2) 回應正確回報 created==1、skipped_conflict==1（真的走 except IntegrityError 分支）
    """
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import Session as OrmSession
    from models import BillingInvoice as Invoice

    h = auth_headers(admin)
    plan_id = client.post("/billing/admin/plans", json=PLAN_A, headers=h).json()["id"]
    bob = make_user("inv_bob2", "inv_bob2@test.com", role="reseller")

    client.post("/billing/admin/subscriptions",
                json={"camera_id": 201, "customer_id": reseller.id, "plan_id": plan_id}, headers=h)
    client.post("/billing/admin/subscriptions",
                json={"camera_id": 202, "customer_id": bob.id, "plan_id": plan_id}, headers=h)
    _backdate_all_subscriptions(db)

    # 注意：不能單純用「第 N 次 flush」計數——SQLAlchemy autoflush 在一般查詢前
    # 也會呼叫 session.flush()，計數會被其他無關查詢（找 admin、找方案…）打亂。
    # 改成只在 session 裡真的有 bob 的待寫入 BillingInvoice 時才讓這次 flush 失敗，
    # 讓 reseller 那筆能先在自己的 savepoint 裡正常 flush + 結束。
    original_flush = OrmSession.flush

    def flaky_flush(self, *args, **kwargs):
        for obj in list(self.new):
            if isinstance(obj, Invoice) and obj.customer_id == bob.id:
                raise IntegrityError("simulated unique violation", params=None, orig=Exception("dup"))
        return original_flush(self, *args, **kwargs)

    monkeypatch.setattr(OrmSession, "flush", flaky_flush)

    r = client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["created"] == 1
    assert body["skipped_conflict"] == 1

    monkeypatch.undo()  # 之後查 DB 用真正的 flush

    assert db.query(Invoice).filter(
        Invoice.customer_id == reseller.id, Invoice.period == PERIOD,
    ).count() == 1
    assert db.query(Invoice).filter(
        Invoice.customer_id == bob.id, Invoice.period == PERIOD,
    ).count() == 0


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


# ── Critical 1：入會當月免費、依 started_at/cancelled_at 篩選訂閱 ──

def test_本月剛加入的訂閱本月不開發票(client, admin, reseller, auth_headers):
    """設計文件§5：入會當月免費，帳單從下一期才開始收。two_subs 之所以要
    刻意回填 started_at，就是因為『本月剛訂閱』預設不該被這期的產生作業計費。

    注意：不可用硬編的 PERIOD 常數（值為 2026-08）當「當月」——時間一過，
    PERIOD 就不再是「現在」，這條測試會悄悄變成在測完全不同的情境（甚至
    可能變成測『訂閱存在之前的期別』而巧合通過）。改用 period_of(utcnow())
    動態算出真正的當月期別。"""
    from services.billing_calc import period_of
    current_period = period_of(datetime.utcnow())

    h = auth_headers(admin)
    pid = client.post("/billing/admin/plans", json=PLAN_A, headers=h).json()["id"]
    client.post("/billing/admin/subscriptions",
                json={"camera_id": 501, "customer_id": reseller.id, "plan_id": pid}, headers=h)
    # 不回填 started_at：維持模型預設值（建立當下＝現在，落在當月）

    r = client.post(f"/billing/admin/invoices/generate/{current_period}", headers=h)
    assert r.status_code == 200
    assert r.json()["created"] == 0
    assert client.get(f"/billing/admin/invoices?period={current_period}", headers=h).json() == []


def test_取消時間剛好等於期別起點不計費該期(client, two_subs, reseller, db):
    """邊界測試：cancelled_at 剛好等於 period_start（而非早於或晚於），
    比照 started_at < period_start 的『嚴格』語意，判定為該期別一刻都沒生效過，
    不該被計費。回歸測試對象是 cancelled_at > period_start（而非 >=）。"""
    from models import BillingSubscription
    from services.billing_calc import period_bounds_utc

    period_start, _ = period_bounds_utc(PERIOD)
    subs = db.query(BillingSubscription).all()
    target = subs[0]
    target.cancelled_at = period_start  # 恰好等於期別起點
    target.status = "cancelled"
    db.commit()

    client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=two_subs)
    inv = client.get(f"/billing/admin/invoices?period={PERIOD}", headers=two_subs).json()[0]
    assert inv["total"] == 800  # 只有另一台相機（未取消）的月費，取消那台不計費


def test_訂閱存在之前的期別不被計費(client, two_subs, reseller):
    """對一個 2026 年才建立的訂閱，去產生 2020-01 的發票，該客戶不該有任何發票——
    這是『期別早於訂閱存在之前』的邊界情況。"""
    r = client.post("/billing/admin/invoices/generate/2020-01", headers=two_subs)
    assert r.status_code == 200
    assert r.json()["created"] == 0
    assert client.get("/billing/admin/invoices?period=2020-01", headers=two_subs).json() == []


# ── Critical 2：逐客戶 savepoint，直接觸發部分唯一索引 ──

def test_直接觸發發票部分唯一索引(db):
    """不透過 monkeypatch，直接對 DB 塞兩筆同 customer_id+period 的非 void 發票，
    驗證『部分唯一索引』本身真的擋得住——而不是只測到繞道模擬的行為。"""
    from sqlalchemy.exc import IntegrityError
    from models import BillingInvoice

    db.add(BillingInvoice(customer_id=1, period="2026-08", total=100, status="unpaid"))
    db.commit()
    db.add(BillingInvoice(customer_id=1, period="2026-08", total=200, status="unpaid"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_兩張void發票可以共存(db):
    """作廢的發票不佔用部分唯一索引，同一客戶＋期別可以有多張 void 發票。"""
    from models import BillingInvoice

    db.add(BillingInvoice(customer_id=1, period="2026-08", total=100, status="void"))
    db.add(BillingInvoice(customer_id=1, period="2026-08", total=200, status="void"))
    db.commit()  # 不應拋出 IntegrityError

    assert db.query(BillingInvoice).filter(
        BillingInvoice.customer_id == 1, BillingInvoice.period == "2026-08",
    ).count() == 2


# ── Task 3：自訂月費進入發票 ──

def test_自訂月費覆蓋方案月費(client, two_subs, reseller, auth_headers, admin, db):
    h = two_subs
    client.put(f"/billing/admin/customers/{reseller.id}", headers=h, json={"custom_monthly_fee": 500})
    client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=h)

    inv = client.get(f"/billing/admin/invoices?period={PERIOD}", headers=h).json()[0]
    assert inv["total"] == 1000          # 兩台相機各 500，而不是 1200+800
    detail = client.get(f"/billing/admin/invoices/{inv['id']}", headers=h).json()
    assert [l["amount"] for l in detail["lines"]] == [500, 500]


def test_自訂月費為零時整張發票為零(client, two_subs, reseller):
    h = two_subs
    client.put(f"/billing/admin/customers/{reseller.id}", headers=h, json={"custom_monthly_fee": 0})
    client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=h)
    inv = client.get(f"/billing/admin/invoices?period={PERIOD}", headers=h).json()[0]
    assert inv["total"] == 0


def test_自訂月費是開立當下的快照(client, two_subs, reseller):
    h = two_subs
    client.put(f"/billing/admin/customers/{reseller.id}", headers=h, json={"custom_monthly_fee": 500})
    client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=h)
    # 開票後改自訂月費，已開立的發票不得跟著變
    client.put(f"/billing/admin/customers/{reseller.id}", headers=h, json={"custom_monthly_fee": 9999})
    inv = client.get(f"/billing/admin/invoices?period={PERIOD}", headers=h).json()[0]
    assert inv["total"] == 1000


def test_作廢後重開才會用新價格_舊發票仍保留舊快照(client, two_subs, reseller):
    """只驗證「欄位有存到值」不足以證明 generate_invoices 真的不會回頭改舊發票——
    idempotency 檢查是「該期別已有非 void 發票就整批跳過」，所以要先把舊發票作廢，
    才能讓 generate_invoices 對同一個期別真的重新跑一次，藉此驗證：
    - 新開的發票用新價格
    - 被作廢的舊發票（快照）不會被舊資料以外的東西動到
    """
    h = two_subs
    client.put(f"/billing/admin/customers/{reseller.id}", headers=h, json={"custom_monthly_fee": 500})
    client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=h)
    old_inv = client.get(f"/billing/admin/invoices?period={PERIOD}", headers=h).json()[0]
    # two_subs 有兩台相機（兩筆訂閱明細），custom_monthly_fee 對每一筆明細各自生效，
    # 所以總額是 500 * 2 = 1000，不是 500。
    assert old_inv["total"] == 1000

    client.post(f"/billing/admin/invoices/{old_inv['id']}/void", headers=h)
    client.put(f"/billing/admin/customers/{reseller.id}", headers=h, json={"custom_monthly_fee": 9999})
    r = client.post(f"/billing/admin/invoices/generate/{PERIOD}", headers=h)
    assert r.json()["created"] == 1

    invs = client.get(f"/billing/admin/invoices?period={PERIOD}", headers=h).json()
    assert len(invs) == 2
    by_id = {i["id"]: i for i in invs}
    assert by_id[old_inv["id"]]["status"] == "void"
    assert by_id[old_inv["id"]]["total"] == 1000  # 舊發票快照沒被改動
    new_inv = [i for i in invs if i["id"] != old_inv["id"]][0]
    assert new_inv["total"] == 9999 * 2  # 新發票用新價格（兩筆明細各自套用）
