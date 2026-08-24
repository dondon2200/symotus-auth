"""_sync_jobs_with_spark：相機縮時 job 在查詢時與 Spark 對齊。

背景：相機縮時的進度原本只有前端浮動卡片會回寫，使用者關掉分頁後 DB 就永遠
定格（實際案例：Spark 已 completed，DB 仍是 processing/29%，因此拿不到下載鈕）。
這裡不碰 DB schema——直接對 helper 餵假的 job 物件與假的 Spark 回應。
"""
import asyncio
from datetime import datetime

import pytest

from routers import jobs as jobs_module
from routers.jobs import parse_spark_completed_at


class FakeJob:
    def __init__(self, job_id, status="processing", percent_complete=0,
                 image_count=None, error_message=None):
        self.job_id = job_id
        self.status = status
        self.percent_complete = percent_complete
        self.image_count = image_count
        self.error_message = error_message
        self.updated_at = None
        self.completed_at = None
        self.video_duration_secs = None


class FakeDB:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _patch_spark(monkeypatch, by_job_id):
    """把 httpx.AsyncClient 換掉，依 job_id 回傳預設 payload；值為 None 代表連線失敗。"""

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

    monkeypatch.setattr(jobs_module.httpx, "AsyncClient", _FakeClient)


def _run(db, jobs):
    asyncio.run(jobs_module._sync_jobs_with_spark(db, jobs))


class TestParseSparkCompletedAt:
    """parse_spark_completed_at：Spark completed_at 一律轉成 naive UTC。"""

    def test_z後綴utc字串(self):
        now = datetime(2026, 8, 21, 12, 0, 0)
        result = parse_spark_completed_at("2026-08-19T03:04:05Z", now)
        assert result == datetime(2026, 8, 19, 3, 4, 5)

    def test_帶時區offset字串換算成utc(self):
        now = datetime(2026, 8, 21, 12, 0, 0)
        # +08:00 的 11:04:05 換成 UTC 是 03:04:05
        result = parse_spark_completed_at("2026-08-19T11:04:05+08:00", now)
        assert result == datetime(2026, 8, 19, 3, 4, 5)

    def test_none回退用now(self):
        now = datetime(2026, 8, 21, 12, 0, 0)
        assert parse_spark_completed_at(None, now) == now

    def test_無法解析回退用now(self):
        now = datetime(2026, 8, 21, 12, 0, 0)
        assert parse_spark_completed_at("not-a-date", now) == now
        assert parse_spark_completed_at("", now) == now

    def test_未來時間夾在now(self):
        now = datetime(2026, 8, 21, 12, 0, 0)
        future = "2099-01-01T00:00:00Z"
        assert parse_spark_completed_at(future, now) == now


def test_completed_spark_job的completed_at採spark回報值(monkeypatch):
    """轉為 completed 時，completed_at 應優先採 Spark 回應的真實完成時間，
    而不是 helper 執行當下（同步時刻）的時間。"""
    job = FakeJob("bv-realtime", status="processing", percent_complete=80)
    _patch_spark(monkeypatch, {"bv-realtime": {
        "status": "completed", "percent_complete": 100, "image_count": 10,
        "error": None, "completed_at": "2026-08-19T03:04:05Z",
    }})
    db = FakeDB()

    _run(db, [job])

    assert job.status == "completed"
    assert job.completed_at == datetime(2026, 8, 19, 3, 4, 5)


def test_completed_spark_job的video_duration_secs存進db(monkeypatch):
    """轉為 completed 時，Spark 回報的 video_duration_secs（產出影片的真實長度）
    要存進 job.video_duration_secs——計費用量以此為準，不可再用 image_count/fps 推算。"""
    job = FakeJob("bv-duration", status="processing", percent_complete=80)
    _patch_spark(monkeypatch, {"bv-duration": {
        "status": "completed", "percent_complete": 100, "image_count": 7194,
        "error": None, "completed_at": "2026-08-18T05:43:10.557398Z",
        "video_duration_secs": 125.43333333333334,
    }})
    db = FakeDB()

    _run(db, [job])

    assert job.video_duration_secs == 125.43333333333334


def test_completed_spark_job缺video_duration_secs時存None不拋錯(monkeypatch):
    job = FakeJob("bv-noduration", status="processing", percent_complete=80)
    _patch_spark(monkeypatch, {"bv-noduration": {
        "status": "completed", "percent_complete": 100, "image_count": 10, "error": None,
    }})
    db = FakeDB()

    _run(db, [job])

    assert job.status == "completed"
    assert job.video_duration_secs is None


def test_completed_spark_job缺completed_at時退回同步時間估計值(monkeypatch):
    job = FakeJob("bv-noeta", status="processing", percent_complete=80)
    _patch_spark(monkeypatch, {"bv-noeta": {
        "status": "completed", "percent_complete": 100, "image_count": 10, "error": None,
    }})
    db = FakeDB()
    before = datetime.utcnow()

    _run(db, [job])

    after = datetime.utcnow()
    assert job.status == "completed"
    assert before <= job.completed_at <= after


def test_大小寫混雜的db狀態不會被誤判成剛轉態(monkeypatch):
    """DB 若存 mixed-case 的 'Completed'（歷史資料 / 外部寫入），active 篩選用
    lower() 已經會把它當終態排除掉，不會查 Spark；這裡直接對 helper 內層邏輯
    做防禦性驗證：就算它跑到轉態比對那一行，lower() 一致比對也不該把
    'Completed' vs 'completed' 誤判成「剛轉態」而重寫 completed_at。"""
    job = FakeJob("bv-mixedcase", status="Completed", percent_complete=100)
    _patch_spark(monkeypatch, {"bv-mixedcase": {
        "status": "completed", "percent_complete": 100, "image_count": 5, "error": None,
        "completed_at": "2026-08-01T00:00:00Z",
    }})
    db = FakeDB()

    _run(db, [job])

    # 已經是（大小寫不同的）completed，不該被視為「剛轉態」而重寫 completed_at。
    assert job.completed_at is None
    assert db.commits == 0


def test_completed_spark_job_is_written_back(monkeypatch):
    """回歸案例：Spark 已完成但 DB 卡在 processing/29% → 應同步成 completed/100%。"""
    job = FakeJob("bv-stuck", status="processing", percent_complete=29)
    _patch_spark(monkeypatch, {"bv-stuck": {
        "status": "completed", "percent_complete": 100, "image_count": 4612, "error": None,
    }})
    db = FakeDB()

    _run(db, [job])

    assert job.status == "completed"
    assert job.percent_complete == 100
    assert job.image_count == 4612
    assert job.updated_at is not None
    assert db.commits == 1


def test_progress_advances_but_never_goes_backwards(monkeypatch):
    job_forward = FakeJob("bv-forward", percent_complete=29)
    job_stale = FakeJob("bv-stale", percent_complete=80)
    _patch_spark(monkeypatch, {
        "bv-forward": {"status": "processing", "percent_complete": 55},
        "bv-stale": {"status": "processing", "percent_complete": 40},
    })

    _run(FakeDB(), [job_forward, job_stale])

    assert job_forward.percent_complete == 55
    assert job_stale.percent_complete == 80, "Spark 回報較低的百分比不應讓進度倒退"


def test_failed_spark_job_records_error(monkeypatch):
    job = FakeJob("bv-bad", percent_complete=12)
    _patch_spark(monkeypatch, {"bv-bad": {
        "status": "failed", "percent_complete": 12, "error": "ffmpeg exited 1",
    }})

    _run(FakeDB(), [job])

    assert job.status == "failed"
    assert job.error_message == "ffmpeg exited 1"


@pytest.mark.parametrize("payload", [None, "missing"])
def test_spark_unreachable_keeps_db_values(monkeypatch, payload):
    """Spark 逾時／查不到時沿用 DB 原值，且不得丟出例外（列表 API 不能因此失敗）。"""
    job = FakeJob("bv-x", status="processing", percent_complete=29)
    _patch_spark(monkeypatch, {"bv-x": payload})
    db = FakeDB()

    _run(db, [job])

    assert job.status == "processing"
    assert job.percent_complete == 29
    assert job.updated_at is None
    assert db.commits == 0


class TestUpdateJobPutVerifiesTerminalStatusWithSpark:
    """PUT /jobs/{id} 的終態改向 Spark 查證，不再信任呼叫端宣稱的值。

    使用真實的 FastAPI TestClient（而非 FakeJob/FakeDB）：這裡要驗證的是
    endpoint 本身的分支邏輯（是否真的呼叫 Spark、是否真的沒改 DB），
    比 _sync_jobs_with_spark 的批次同步更貼近使用者可觸發的路徑。
    """

    @pytest.fixture()
    def app(self):
        from fastapi import FastAPI
        from routers.jobs import router as jobs_router
        a = FastAPI()
        a.include_router(jobs_router)
        return a

    @pytest.fixture()
    def user(self, make_user):
        return make_user("spark_put_user", "spark_put_user@test.com", role="reseller")

    def _make_job(self, db, user_id, job_id, status="processing"):
        from models import TimelapsJob
        j = TimelapsJob(user_id=user_id, job_id=job_id, camera_id=1, status=status)
        db.add(j); db.commit(); db.refresh(j)
        return j

    def test_spark確認completed採spark的完成時間與影片長度(self, client, user, auth_headers, db, monkeypatch):
        """呼叫端刻意送假的 completed_at／video_duration_secs，斷言最後存的是 Spark 那個。"""
        from models import TimelapsJob
        self._make_job(db, user.id, "put-ok", status="processing")
        _patch_spark(monkeypatch, {"put-ok": {
            "status": "completed",
            "completed_at": "2026-08-19T03:04:05Z",
            "video_duration_secs": 88.5,
        }})

        r = client.put("/jobs/put-ok", json={
            "status": "completed", "percent_complete": 100,
            "video_duration_secs": 999.0,  # 呼叫端亂送的假值，應被忽略
        }, headers=auth_headers(user))
        assert r.status_code == 200

        job = db.query(TimelapsJob).filter(TimelapsJob.job_id == "put-ok").first()
        assert job.status == "completed"
        assert job.completed_at == datetime(2026, 8, 19, 3, 4, 5)
        assert job.video_duration_secs == 88.5

    def test_spark說還在處理中呼叫端宣稱completed不改狀態且有warning(self, client, user, auth_headers, db, monkeypatch, caplog):
        from models import TimelapsJob
        self._make_job(db, user.id, "put-lying", status="processing")
        _patch_spark(monkeypatch, {"put-lying": {"status": "processing", "percent_complete": 40}})

        import logging
        with caplog.at_level(logging.WARNING, logger="routers.jobs"):
            r = client.put("/jobs/put-lying", json={"status": "completed", "percent_complete": 100},
                            headers=auth_headers(user))
        assert r.status_code == 200

        job = db.query(TimelapsJob).filter(TimelapsJob.job_id == "put-lying").first()
        assert job.status == "processing"
        assert job.completed_at is None
        assert any("疑似偽報" in rec.message for rec in caplog.records)

    def test_spark查不到或逾時狀態不變(self, client, user, auth_headers, db, monkeypatch, caplog):
        from models import TimelapsJob
        self._make_job(db, user.id, "put-unreachable", status="processing")

        class _Boom:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, *a, **kw):
                raise RuntimeError("spark unreachable")

        monkeypatch.setattr(jobs_module.httpx, "AsyncClient", _Boom)

        import logging
        with caplog.at_level(logging.WARNING, logger="routers.jobs"):
            r = client.put("/jobs/put-unreachable", json={"status": "completed", "percent_complete": 100},
                            headers=auth_headers(user))
        assert r.status_code == 200

        job = db.query(TimelapsJob).filter(TimelapsJob.job_id == "put-unreachable").first()
        assert job.status == "processing"
        assert job.completed_at is None
        assert any("無法查證" in rec.message for rec in caplog.records)

    def test_非終態的put不打spark且percent正常更新(self, client, user, auth_headers, db, monkeypatch):
        from models import TimelapsJob
        self._make_job(db, user.id, "put-processing", status="processing")

        class _Boom:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, *a, **kw):
                raise AssertionError("非終態 PUT 不該查詢 Spark")

        monkeypatch.setattr(jobs_module.httpx, "AsyncClient", _Boom)

        r = client.put("/jobs/put-processing", json={"status": "processing", "percent_complete": 55},
                        headers=auth_headers(user))
        assert r.status_code == 200

        job = db.query(TimelapsJob).filter(TimelapsJob.job_id == "put-processing").first()
        assert job.status == "processing"
        assert job.percent_complete == 55

    def test_已經completed的任務再put_completed_at不被覆蓋(self, client, user, auth_headers, db, monkeypatch):
        from models import TimelapsJob
        job = self._make_job(db, user.id, "put-already-done", status="completed")
        original_completed_at = datetime(2026, 1, 1, 0, 0, 0)
        job.completed_at = original_completed_at
        db.commit()

        _patch_spark(monkeypatch, {"put-already-done": {
            "status": "completed",
            "completed_at": "2026-08-19T03:04:05Z",  # Spark 給的新時間，不該被採用
        }})

        r = client.put("/jobs/put-already-done", json={"status": "completed", "percent_complete": 100},
                        headers=auth_headers(user))
        assert r.status_code == 200

        db.refresh(job)
        assert job.completed_at == original_completed_at


def test_terminal_jobs_are_not_queried(monkeypatch):
    """已完成／已失敗的 job 不該再打 Spark。"""
    called = []

    class _Boom:
        def __init__(self, *a, **kw):
            called.append(1)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **kw):
            raise AssertionError("終態 job 不應查詢 Spark")

    monkeypatch.setattr(jobs_module.httpx, "AsyncClient", _Boom)
    db = FakeDB()

    _run(db, [FakeJob("a", status="completed", percent_complete=100),
              FakeJob("b", status="failed")])

    assert called == []
    assert db.commits == 0
