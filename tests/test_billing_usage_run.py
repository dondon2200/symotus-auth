"""採集流程。NAS 與 Camera Backend 都以 monkeypatch 取代，測的是流程本身。"""
from datetime import datetime

import pytest

from models import TimelapsJob, BillingUsageDaily, BillingPlan, BillingSubscription
import services.billing_usage as usage

DAY = "2026-08-19"


@pytest.fixture()
def customer(make_user):
    return make_user("run_user", "run@test.com", role="reseller")


@pytest.fixture()
def subs(db, customer):
    plan = BillingPlan(name="標準", monthly_fee=1200, timelapse_quota_secs=3600, storage_quota_gb=100)
    db.add(plan); db.commit(); db.refresh(plan)
    for cam, serial in [(1, "SN001"), (2, "SN002")]:
        db.add(BillingSubscription(camera_id=cam, customer_id=customer.id,
                                   plan_id=plan.id, status="active", camera_serial=serial))
    db.commit()
    return plan


@pytest.mark.anyio
async def test_採集寫入每台相機一列(db, customer, subs, monkeypatch):
    monkeypatch.setattr(usage, "collect_storage_gb", lambda serial, base=None: {"SN001": 1.5, "SN002": 2.5}[serial])
    db.add(TimelapsJob(user_id=customer.id, job_id="j1", camera_id=1, status="completed",
                       image_count=900, fps=30, created_at=datetime(2026, 8, 19, 10, 0)))
    db.commit()

    result = await usage.run_collection(db, DAY)

    assert result["cameras"] == 2
    assert result["failed"] == 0
    rows = {r.camera_id: r for r in db.query(BillingUsageDaily).all()}
    assert rows[1].timelapse_secs == 30
    assert rows[1].storage_gb == 1.5
    assert rows[2].timelapse_secs == 0      # 沒有縮時任務也要有一列，代表「已採集且為 0」
    assert rows[2].storage_gb == 2.5


@pytest.mark.anyio
async def test_單台失敗不影響其他相機(db, customer, subs, monkeypatch):
    def flaky(serial, base=None):
        if serial == "SN001":
            raise OSError("NAS 掛載中斷")
        return 2.5
    monkeypatch.setattr(usage, "collect_storage_gb", flaky)

    result = await usage.run_collection(db, DAY)

    assert result["failed"] == 1
    assert result["cameras"] == 1
    rows = db.query(BillingUsageDaily).all()
    assert len(rows) == 1
    assert rows[0].camera_id == 2


@pytest.mark.anyio
async def test_取消的訂閱不採集(db, customer, subs, monkeypatch):
    monkeypatch.setattr(usage, "collect_storage_gb", lambda serial, base=None: 1.0)
    sub = db.query(BillingSubscription).filter(BillingSubscription.camera_id == 1).first()
    sub.status = "cancelled"
    db.commit()

    result = await usage.run_collection(db, DAY)
    assert result["cameras"] == 1


@pytest.mark.anyio
async def test_重跑同一天不產生重複列(db, customer, subs, monkeypatch):
    monkeypatch.setattr(usage, "collect_storage_gb", lambda serial, base=None: 1.0)
    await usage.run_collection(db, DAY)
    await usage.run_collection(db, DAY)
    assert db.query(BillingUsageDaily).count() == 2   # 兩台相機各一列，不是四列
