"""backfill_missing_video_duration_secs：collect_timelapse_secs 的補值安全網。

情境：前端 PUT /jobs/{id} 常常先到且不帶 video_duration_secs，把值定成 None
（見 test_jobs_video_duration_writeback.py）。採集流程在算當天用量之前，先對
缺值的 completed job 問 Spark 補回真值並寫回 DB，查不到的單筆不中斷整批。
"""
from datetime import datetime

import pytest

from models import TimelapsJob
import services.billing_usage as usage

DAY = "2026-08-19"


@pytest.fixture()
def user(make_user):
    return make_user("backfill_user", "backfill@test.com", role="reseller")


def _job(db, user_id, job_id, video_duration_secs=None, image_count=900, fps=30,
         created_at=None, status="completed"):
    j = TimelapsJob(user_id=user_id, job_id=job_id, camera_id=1, status=status,
                     image_count=image_count, fps=fps,
                     created_at=created_at or datetime(2026, 8, 19, 10, 0),
                     video_duration_secs=video_duration_secs)
    db.add(j); db.commit(); db.refresh(j)
    return j


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _patch_spark(monkeypatch, by_job_id):
    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            job_id = url.rsplit("/", 1)[-1]
            payload = by_job_id.get(job_id, "missing")
            if payload is None:
                raise RuntimeError("spark unreachable")
            if payload == "missing":
                return _FakeResponse(404, {})
            return _FakeResponse(200, payload)

    monkeypatch.setattr(usage.httpx, "AsyncClient", _FakeClient)


@pytest.mark.anyio
async def test_補值成功寫回db(db, user, monkeypatch):
    _job(db, user.id, "j1", video_duration_secs=None)
    _patch_spark(monkeypatch, {"j1": {"video_duration_secs": 88.2}})

    filled = await usage.backfill_missing_video_duration_secs(db, DAY)

    assert filled == 1
    job = db.query(TimelapsJob).filter(TimelapsJob.job_id == "j1").first()
    assert job.video_duration_secs == 88.2


@pytest.mark.anyio
async def test_已有值的job不查spark(db, user, monkeypatch):
    _job(db, user.id, "j2", video_duration_secs=10.0)
    called = []

    class _Boom:
        def __init__(self, *a, **kw):
            called.append(1)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **kw):
            raise AssertionError("不該查已有值的 job")

    monkeypatch.setattr(usage.httpx, "AsyncClient", _Boom)

    filled = await usage.backfill_missing_video_duration_secs(db, DAY)

    assert filled == 0
    assert called == []


@pytest.mark.anyio
async def test_單筆spark查詢失敗不中斷整批(db, user, monkeypatch):
    _job(db, user.id, "ok", video_duration_secs=None)
    _job(db, user.id, "boom", video_duration_secs=None)
    _patch_spark(monkeypatch, {"ok": {"video_duration_secs": 30.0}, "boom": None})

    filled = await usage.backfill_missing_video_duration_secs(db, DAY)

    assert filled == 1
    jobs = {j.job_id: j for j in db.query(TimelapsJob).all()}
    assert jobs["ok"].video_duration_secs == 30.0
    assert jobs["boom"].video_duration_secs is None


@pytest.mark.anyio
async def test_put先到寫None之後補值路徑補上真值(db, user, monkeypatch):
    """完整回歸：模擬前端 PUT 已先把任務轉 completed 且 video_duration_secs
    留 None（沿用 routers/jobs.py 的行為），採集前的補值安全網要能把它填上。"""
    job = _job(db, user.id, "put-then-backfill", video_duration_secs=None, image_count=7194, fps=30)
    _patch_spark(monkeypatch, {"put-then-backfill": {"video_duration_secs": 125.4}})

    filled = await usage.backfill_missing_video_duration_secs(db, DAY)
    assert filled == 1

    secs = usage.collect_timelapse_secs(db, DAY)
    assert secs == {1: 125}  # 補值後採真值，而非 image_count/fps 的高估值(239秒)


@pytest.mark.anyio
async def test_run_collection會先跑補值(db, user, monkeypatch):
    from models import BillingPlan, BillingSubscription

    plan = BillingPlan(name="標準", monthly_fee=1200, timelapse_quota_secs=3600, storage_quota_gb=100)
    db.add(plan); db.commit(); db.refresh(plan)
    db.add(BillingSubscription(camera_id=1, customer_id=user.id, plan_id=plan.id,
                                status="active", camera_serial="SN001"))
    db.commit()

    _job(db, user.id, "j-run", video_duration_secs=None, image_count=7194, fps=30)
    _patch_spark(monkeypatch, {"j-run": {"video_duration_secs": 125.4}})
    monkeypatch.setattr(usage, "collect_storage_gb", lambda serial, base=None: 1.0)

    async def _fake_fetch_collector_token(timeout: float = 15) -> str:
        return ""
    monkeypatch.setattr(usage, "fetch_collector_token", _fake_fetch_collector_token)

    await usage.run_collection(db, DAY)

    job = db.query(TimelapsJob).filter(TimelapsJob.job_id == "j-run").first()
    assert job.video_duration_secs == 125.4

    from models import BillingUsageDaily
    row = db.query(BillingUsageDaily).filter(BillingUsageDaily.camera_id == 1).first()
    assert row.timelapse_secs == 125
