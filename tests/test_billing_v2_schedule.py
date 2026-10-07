"""帳單排程（spec §7）：冪等、補開、續約、不續約、上限、鎖定狀態。"""
from datetime import date

import pytest

from models import BillingBill, BillingPlanV2, BillingSubscriptionV2
from services.billing_dates import add_months
from services.billing_v2 import (
    MAX_NEW_BILLS_PER_RUN, advance_subscription, assert_camera_unlocked, camera_billing_flags,
    get_lock_map, invalidate_lock_cache, locked_serial_in_path,
)

D = date


@pytest.fixture()
def reseller(make_user):
    return make_user("r1", "r1@test.com", role="reseller")


@pytest.fixture()
def plan(db):
    p = BillingPlanV2(name="標準", term="monthly", cycle="monthly", price=1500)
    db.add(p)
    db.commit()
    return p


def _sub(db, reseller, plan, start, term="monthly", cycle="monthly", camera_id=44, **kw):
    s = BillingSubscriptionV2(
        camera_id=camera_id, camera_name=f"cam{camera_id}", customer_id=reseller.id, plan_id=plan.id,
        plan_name=plan.name, term=term, cycle=cycle, price=kw.pop("price", 1500), start_date=start,
        anchor_day=start.day, term_start=start, status=kw.pop("status", "active"), **kw)
    db.add(s)
    db.commit()
    return s


def _bills(db, sub):
    db.flush()
    return db.query(BillingBill).filter(BillingBill.subscription_id == sub.id).order_by(
        BillingBill.period_start).all()


def test_建立當天開第一張(db, reseller, plan):
    s = _sub(db, reseller, plan, D(2026, 10, 7))
    assert advance_subscription(db, s, D(2026, 10, 7)) == 1
    db.commit()
    b = _bills(db, s)[0]
    assert (b.period_start, b.period_end, b.due_date, b.amount) == (
        D(2026, 10, 7), D(2026, 11, 7), D(2026, 11, 7), 1500)


def test_同一天跑兩次只有一張(db, reseller, plan):
    s = _sub(db, reseller, plan, D(2026, 10, 7))
    advance_subscription(db, s, D(2026, 10, 7))
    db.commit()
    assert advance_subscription(db, s, D(2026, 10, 7)) == 0
    db.commit()
    assert len(_bills(db, s)) == 1


def test_停擺補開_帳單日不順延(db, reseller, plan):
    s = _sub(db, reseller, plan, D(2026, 10, 7))
    advance_subscription(db, s, D(2026, 10, 7))
    db.commit()
    # 11/06–11/09 停機，11/09 才補跑
    assert advance_subscription(db, s, D(2026, 11, 9)) == 1
    db.commit()
    b = _bills(db, s)[1]
    assert (b.period_start, b.due_date) == (D(2026, 11, 7), D(2026, 12, 7))


def test_預定訂閱到起始日才轉生效(db, reseller, plan):
    s = _sub(db, reseller, plan, D(2026, 10, 20), status="scheduled")
    assert advance_subscription(db, s, D(2026, 10, 19)) == 0
    assert s.status == "scheduled"
    assert advance_subscription(db, s, D(2026, 10, 20)) == 1
    assert s.status == "active"


def test_暫停期間照開帳單(db, reseller, plan):
    s = _sub(db, reseller, plan, D(2026, 10, 7), status="suspended")
    assert advance_subscription(db, s, D(2026, 11, 7)) == 2


def test_已結束不再開(db, reseller, plan):
    s = _sub(db, reseller, plan, D(2026, 10, 7), status="ended")
    assert advance_subscription(db, s, D(2026, 12, 7)) == 0


def test_月約不續約_生效日轉ended且不開當天帳單(db, reseller, plan):
    s = _sub(db, reseller, plan, D(2026, 10, 7), cancel_at=D(2026, 11, 7))
    advance_subscription(db, s, D(2026, 11, 7))
    assert s.status == "ended" and s.end_reason == "not_renewed"
    assert [b.period_start for b in _bills(db, s)] == [D(2026, 10, 7)]


def test_年約月繳_12期後自動續約(db, reseller, plan):
    s = _sub(db, reseller, plan, D(2026, 10, 7), term="annual", cycle="monthly", auto_renew=True, price=1300)
    advance_subscription(db, s, D(2027, 10, 7))
    assert s.status == "active"
    assert s.term_start == D(2027, 10, 7)
    assert len(_bills(db, s)) == 13


def test_年約不續約_到期日結束不開第13張(db, reseller, plan):
    s = _sub(db, reseller, plan, D(2026, 10, 7), term="annual", cycle="monthly", auto_renew=False)
    advance_subscription(db, s, D(2027, 10, 7))
    assert s.status == "ended" and s.end_reason == "not_renewed"
    assert len(_bills(db, s)) == 12


def test_年約年繳_一年一張(db, reseller, plan):
    s = _sub(db, reseller, plan, D(2026, 10, 7), term="annual", cycle="yearly", price=15000)
    advance_subscription(db, s, D(2027, 10, 7))
    bills = _bills(db, s)
    assert [(b.period_start, b.due_date) for b in bills] == [
        (D(2026, 10, 7), D(2026, 11, 7)), (D(2027, 10, 7), D(2027, 11, 7))]


def test_補開上限(db, reseller, plan):
    start = D(2020, 1, 1)
    s = _sub(db, reseller, plan, start)
    n = advance_subscription(db, s, add_months(start, 40, 1))
    assert n == MAX_NEW_BILLS_PER_RUN


# ── 鎖定狀態 ──

def test_鎖定只看最新一份訂閱(db, reseller, plan, make_user):
    old = _sub(db, reseller, plan, D(2026, 8, 1), status="ended", camera_serial="SER1")
    invalidate_lock_cache()
    assert get_lock_map(db)[44]["state"] == "ended"
    assert locked_serial_in_path(db, "/homes/firmness/SER1/2026-09-01/120000.jpg")
    assert not locked_serial_in_path(db, "/homes/firmness/SER10/x.jpg")
    # 重新開通（新訂閱，即使是 scheduled）就立即解鎖（spec S12b）
    _sub(db, reseller, plan, D(2026, 12, 1), status="scheduled")
    invalidate_lock_cache()
    assert 44 not in get_lock_map(db)
    assert old.id  # 舊訂閱留作歷史


def test_解除鎖定(db, reseller, plan):
    from datetime import datetime
    s = _sub(db, reseller, plan, D(2026, 8, 1), status="ended")
    invalidate_lock_cache()
    assert 44 in get_lock_map(db)
    s.lock_released_at = datetime.utcnow()
    db.commit()
    invalidate_lock_cache()
    assert 44 not in get_lock_map(db)


def test_閘門_admin放行其他人403(db, reseller, plan, make_user):
    from fastapi import HTTPException
    admin = make_user("a", "a@test.com", role="symotus_admin")
    end_user = make_user("e", "e@test.com", role="end_user")
    _sub(db, reseller, plan, D(2026, 8, 1), status="suspended")
    invalidate_lock_cache()
    assert_camera_unlocked(db, admin, 44)
    for u in (reseller, end_user):
        with pytest.raises(HTTPException) as ei:
            assert_camera_unlocked(db, u, 44)
        assert ei.value.status_code == 403
        assert ei.value.detail["code"] == "CAMERA_LOCKED"
        assert ei.value.detail["state"] == "suspended"
    assert_camera_unlocked(db, end_user, 99)  # 未納管相機不受影響


def test_逾期旗標不給end_user(db, reseller, plan, make_user):
    s = _sub(db, reseller, plan, D(2026, 10, 7))
    advance_subscription(db, s, D(2026, 10, 7))
    db.commit()
    end_user = make_user("e", "e@test.com", role="end_user")
    other = make_user("r2", "r2@test.com", role="reseller")
    today = D(2026, 11, 8)
    assert camera_billing_flags(db, reseller, today) == {44: {"overdue": True, "payment_pending": False}}
    assert camera_billing_flags(db, end_user, today) == {}
    assert camera_billing_flags(db, other, today) == {}
    assert camera_billing_flags(db, reseller, D(2026, 11, 7)) == {}  # 截止日當天不算
