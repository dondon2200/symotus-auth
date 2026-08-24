"""PUT /jobs/{id}、/jobs/internal/{id}：video_duration_secs 的補值語意。

背景：前端「使用者盯著卡片看」那條路徑最先打到 PUT /jobs/{id}，過去沒帶
video_duration_secs，若寫入邏輯跟 completed_at 綁同一個「轉態守衛」，
第一次 PUT 就會把它永久定成 None——之後任何補值路徑都補不進來。
修法：video_duration_secs 用「目前為 None 且傳入非 None」判斷，不管是否
剛好是轉態那一刻；completed_at 語意不變（一旦記錄就不再改）。

2026-08-24 起 PUT /jobs/{id} 的終態改向 Spark 查證（見 test_jobs_spark_sync.py），
不再信任呼叫端宣稱的 status/completed_at/video_duration_secs，所以這裡的
每個 PUT 都要 monkeypatch httpx.AsyncClient 讓 Spark 回應與呼叫端宣稱一致
（PUT /jobs/internal/{id} 不受影響，維持原樣直接信任 Spark callback）。
"""
from datetime import datetime

import pytest
from fastapi import FastAPI

from routers import jobs as jobs_module
from routers.jobs import router as jobs_router
from models import TimelapsJob


@pytest.fixture()
def app():
    a = FastAPI()
    a.include_router(jobs_router)
    return a


@pytest.fixture()
def user(make_user):
    return make_user("job_owner", "job_owner@test.com", role="reseller")


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _patch_spark(monkeypatch, by_job_id):
    """把 httpx.AsyncClient 換掉，依 job_id 回傳預設 payload（用於 PUT /jobs/{id} 的終態查證）。"""

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            job_id = url.rsplit("/", 1)[-1]
            payload = by_job_id.get(job_id, {"status": "completed"})
            return _FakeResponse(200, payload)

    monkeypatch.setattr(jobs_module.httpx, "AsyncClient", _FakeClient)


def _make_job(db, user_id, job_id, status="processing", video_duration_secs=None):
    j = TimelapsJob(user_id=user_id, job_id=job_id, camera_id=1, status=status,
                     video_duration_secs=video_duration_secs)
    db.add(j); db.commit(); db.refresh(j)
    return j


def test_put沒帶video_duration_secs轉completed不寫死None以外的行為(client, user, auth_headers, db, monkeypatch):
    _make_job(db, user.id, "j1", status="processing")
    _patch_spark(monkeypatch, {"j1": {"status": "completed"}})

    r = client.put("/jobs/j1", json={"status": "completed", "percent_complete": 100},
                    headers=auth_headers(user))
    assert r.status_code == 200

    job = db.query(TimelapsJob).filter(TimelapsJob.job_id == "j1").first()
    assert job.status == "completed"
    assert job.video_duration_secs is None
    assert job.completed_at is not None


def test_put先到寫None之後補值成功可以覆蓋(client, user, auth_headers, db, monkeypatch):
    """關鍵回歸：第一個 PUT（前端卡片，Spark 尚未回報時長）先到，把
    completed_at 定案、video_duration_secs 留 None；之後補值路徑（模擬第二個
    PUT，這次 Spark 回報了真值）打進來，仍應該把 None 補成真值，且不動 completed_at。"""
    _make_job(db, user.id, "j2", status="processing")
    _patch_spark(monkeypatch, {"j2": {"status": "completed"}})

    r1 = client.put("/jobs/j2", json={"status": "completed", "percent_complete": 100},
                     headers=auth_headers(user))
    assert r1.status_code == 200
    job = db.query(TimelapsJob).filter(TimelapsJob.job_id == "j2").first()
    completed_at_first = job.completed_at
    assert job.video_duration_secs is None

    _patch_spark(monkeypatch, {"j2": {"status": "completed", "video_duration_secs": 125.4}})
    r2 = client.put("/jobs/j2", json={"status": "completed", "percent_complete": 100},
                     headers=auth_headers(user))
    assert r2.status_code == 200

    db.refresh(job)
    assert job.video_duration_secs == 125.4
    assert job.completed_at == completed_at_first  # completed_at 不被後續 PUT 改動


def test_put已有真值不被後續None覆蓋(client, user, auth_headers, db, monkeypatch):
    _make_job(db, user.id, "j3", status="completed", video_duration_secs=99.0)
    _patch_spark(monkeypatch, {"j3": {"status": "completed"}})

    r = client.put("/jobs/j3", json={"status": "completed", "percent_complete": 100},
                    headers=auth_headers(user))
    assert r.status_code == 200

    job = db.query(TimelapsJob).filter(TimelapsJob.job_id == "j3").first()
    assert job.video_duration_secs == 99.0


def test_internal端點同樣的補值語意(client, db, user):
    """/jobs/internal/{id} 是 Spark server-to-server callback，本來就直接信任
    payload，不受本次「PUT /jobs/{id} 改向 Spark 查證」影響，不需要 monkeypatch。"""
    _make_job(db, user.id, "j4", status="processing")

    r1 = client.put("/jobs/internal/j4", json={"status": "completed", "percent_complete": 100},
                     headers={"x-service-key": "spark-callback"})
    assert r1.status_code == 200
    job = db.query(TimelapsJob).filter(TimelapsJob.job_id == "j4").first()
    assert job.video_duration_secs is None
    completed_at_first = job.completed_at

    r2 = client.put("/jobs/internal/j4", json={"status": "completed", "percent_complete": 100,
                                                "video_duration_secs": 42.5},
                     headers={"x-service-key": "spark-callback"})
    assert r2.status_code == 200
    db.refresh(job)
    assert job.video_duration_secs == 42.5
    assert job.completed_at == completed_at_first
