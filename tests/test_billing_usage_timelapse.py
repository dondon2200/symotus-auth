from datetime import datetime

import pytest

from models import TimelapsJob, BillingUsageDaily
from services.billing_usage import collect_timelapse_secs, upsert_usage

DAY = "2026-08-19"


def _job(db, user_id, camera_id, created_at, status="completed", image_count=900, fps=30,
         job_id=None, video_duration_secs=None):
    j = TimelapsJob(
        user_id=user_id, job_id=job_id or f"j{camera_id}-{created_at.isoformat()}",
        camera_id=camera_id, status=status,
        image_count=image_count, fps=fps, created_at=created_at,
        video_duration_secs=video_duration_secs,
    )
    db.add(j); db.commit()
    return j


@pytest.fixture()
def user(make_user):
    return make_user("usage_user", "usage@test.com", role="reseller")


def test_加總當日完成的任務(db, user):
    # 台北 2026-08-19 = naive UTC 2026-08-18 16:00 ~ 2026-08-19 16:00
    _job(db, user.id, 1, datetime(2026, 8, 18, 20, 0), image_count=900, fps=30)   # 30 秒
    _job(db, user.id, 1, datetime(2026, 8, 19, 10, 0), image_count=1800, fps=30)  # 60 秒
    assert collect_timelapse_secs(db, DAY) == {1: 90}


def test_只計完成的任務(db, user):
    _job(db, user.id, 1, datetime(2026, 8, 19, 10, 0), status="processing")
    _job(db, user.id, 1, datetime(2026, 8, 19, 11, 0), status="failed")
    assert collect_timelapse_secs(db, DAY) == {}


def test_台北時區的日界(db, user):
    # naive UTC 15:59 = 台北 23:59 → 屬於 08-19
    _job(db, user.id, 1, datetime(2026, 8, 19, 15, 59), image_count=900, fps=30)
    # naive UTC 16:00 = 台北 隔天 00:00 → 屬於 08-20，不該被算進來
    _job(db, user.id, 1, datetime(2026, 8, 19, 16, 0), image_count=900, fps=30)
    assert collect_timelapse_secs(db, DAY) == {1: 30}


def test_缺image_count的舊資料計零(db, user):
    _job(db, user.id, 1, datetime(2026, 8, 19, 10, 0), image_count=None, fps=30)
    assert collect_timelapse_secs(db, DAY) == {1: 0}


def test_多台相機分開統計(db, user):
    _job(db, user.id, 1, datetime(2026, 8, 19, 10, 0), image_count=900, fps=30)
    _job(db, user.id, 2, datetime(2026, 8, 19, 10, 0), image_count=1800, fps=30)
    assert collect_timelapse_secs(db, DAY) == {1: 30, 2: 60}


def test_以完成時間歸日而非建立時間(db, user):
    # 台北 08-19 23:40 建立（naive UTC 15:40）、08-20 04:00 完成（naive UTC 19:00 前一天）
    # 建立日是 08-19、完成日是 08-20 → 應計入 08-20，不是 08-19
    j = _job(db, user.id, 1, datetime(2026, 8, 19, 15, 40), image_count=900, fps=30)
    j.completed_at = datetime(2026, 8, 19, 20, 0)   # 台北 08-20 04:00
    db.commit()

    assert collect_timelapse_secs(db, "2026-08-19") == {}
    assert collect_timelapse_secs(db, "2026-08-20") == {1: 30}


def test_舊資料沒有完成時間時退回建立時間(db, user):
    # completed_at 是後來才加的欄位，既有資料是 NULL。
    # 若不 fallback，正式庫既有的縮時用量會整批消失。
    j = _job(db, user.id, 1, datetime(2026, 8, 19, 10, 0), image_count=900, fps=30)
    j.completed_at = None
    db.commit()
    assert collect_timelapse_secs(db, DAY) == {1: 30}


def test_完成時間的台北日界(db, user):
    # naive UTC 15:59 = 台北 23:59 → 屬 08-19
    a = _job(db, user.id, 1, datetime(2026, 8, 1, 0, 0), image_count=900, fps=30, job_id="a")
    a.completed_at = datetime(2026, 8, 19, 15, 59)
    # naive UTC 16:00 = 台北隔天 00:00 → 屬 08-20
    b = _job(db, user.id, 1, datetime(2026, 8, 1, 0, 0), image_count=900, fps=30, job_id="b")
    b.completed_at = datetime(2026, 8, 19, 16, 0)
    db.commit()
    assert collect_timelapse_secs(db, DAY) == {1: 30}


def test_video_duration_secs與fallback混合正確加總(db, user):
    # 同一天兩筆任務：一筆有 Spark 回報的實際影片長度（優先採用），
    # 一筆是舊資料沒有該欄位（退回 image_count/fps 的高估值）。
    _job(db, user.id, 1, datetime(2026, 8, 19, 10, 0),
         image_count=7194, fps=30, video_duration_secs=125.43, job_id="has-duration")  # 125 秒
    _job(db, user.id, 1, datetime(2026, 8, 19, 11, 0),
         image_count=900, fps=30, video_duration_secs=None, job_id="no-duration")  # 30 秒（fallback）
    assert collect_timelapse_secs(db, DAY) == {1: 155}


def test_沒有camera_id的任務被忽略(db, user):
    # GDrive 來源的縮時沒有相機，不屬於任何相機的用量
    _job(db, user.id, None, datetime(2026, 8, 19, 10, 0))
    assert collect_timelapse_secs(db, DAY) == {}


def test_同日重跑覆蓋而非累加(db):
    upsert_usage(db, camera_id=1, day=DAY, timelapse_secs=30, storage_gb=1.5)
    upsert_usage(db, camera_id=1, day=DAY, timelapse_secs=90, storage_gb=2.0)

    rows = db.query(BillingUsageDaily).filter(BillingUsageDaily.camera_id == 1).all()
    assert len(rows) == 1           # 不是兩列
    assert rows[0].timelapse_secs == 90   # 覆蓋，不是 120
    assert rows[0].storage_gb == 2.0


def test_不同日各自一列(db):
    upsert_usage(db, camera_id=1, day=DAY, timelapse_secs=30, storage_gb=1.0)
    upsert_usage(db, camera_id=1, day="2026-08-20", timelapse_secs=60, storage_gb=1.2)
    assert db.query(BillingUsageDaily).filter(BillingUsageDaily.camera_id == 1).count() == 2


def test_併發insert撞UNIQUE後改為覆蓋而非拋例外(db, monkeypatch):
    # 模擬情境：本次查詢時該 (camera_id, date) 尚不存在，走 insert 分支；
    # 但另一個執行緒（逾時重試 / 手動補跑重疊）已經搶先插入同一筆，
    # 這裡把第一次 commit 換成先真的插入那筆「別人的資料」再拋 IntegrityError，
    # 藉此重現撞 UNIQUE(camera_id, date) 的情境。
    from sqlalchemy.exc import IntegrityError

    real_commit = db.commit
    calls = {"n": 0}

    def fake_commit():
        calls["n"] += 1
        if calls["n"] == 1:
            # 先讓「另一個執行緒」的資料落地，再模擬本次 commit 撞到 UNIQUE
            db.rollback()
            db.add(BillingUsageDaily(
                camera_id=2, date=DAY,
                timelapse_secs=1, storage_gb=0.1,
                collected_at=datetime(2026, 1, 1),
            ))
            real_commit()
            raise IntegrityError("upsert_usage race", params=None, orig=Exception("dup"))
        return real_commit()

    monkeypatch.setattr(db, "commit", fake_commit)

    upsert_usage(db, camera_id=2, day=DAY, timelapse_secs=90, storage_gb=2.0)

    rows = db.query(BillingUsageDaily).filter(
        BillingUsageDaily.camera_id == 2, BillingUsageDaily.date == DAY
    ).all()
    assert len(rows) == 1                     # 沒有變成重複列
    assert rows[0].timelapse_secs == 90        # 覆蓋成本次要寫的值，不是拋例外
    assert rows[0].storage_gb == 2.0
