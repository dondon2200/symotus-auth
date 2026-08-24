"""採集流程。NAS 與 Camera Backend 都以 monkeypatch 取代，測的是流程本身。"""
from datetime import datetime

import pytest
from fastapi import FastAPI

from routers.billing import router as billing_router
from models import TimelapsJob, BillingUsageDaily, BillingPlan, BillingSubscription
import services.billing_usage as usage

DAY = "2026-08-19"


@pytest.fixture(autouse=True)
def no_real_token_fetch(monkeypatch):
    """這些測試意圖上完全不該打真正的 Camera Backend——過去只靠 CI/本機沒設
    CAMERA_SERVICE_KEY/BILLING_COLLECTOR_CAMERA_EMAIL 讓 fetch_collector_token
    隱性回傳 ""；若哪台機器剛好設了這兩個環境變數，測試會真的打
    https://user.symotus.com/internal/auth/token。明確 monkeypatch 掉，不依賴環境。"""
    async def _fake_fetch_collector_token(timeout: float = 15) -> str:
        return ""
    monkeypatch.setattr(usage, "fetch_collector_token", _fake_fetch_collector_token)


@pytest.fixture()
def app():
    a = FastAPI()
    a.include_router(billing_router)
    return a


@pytest.fixture()
def admin(make_user):
    return make_user("usage_admin", "usage_admin@test.com", role="symotus_admin")


@pytest.fixture()
def reseller(make_user):
    return make_user("usage_reseller", "usage_reseller@test.com", role="reseller")


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
async def test_採集在thread內用自己的session不影響外層session(db, customer, subs, monkeypatch):
    """run_collection 把重活丟進 asyncio.to_thread、thread 內開自己的 SessionLocal
    （不能沿用呼叫端傳入的 db）。這裡驗證兩件事：
    1. 呼叫端傳進去的 db（外層 session）採集後仍可正常查詢、寫入——沒有被
       thread 內的 session 搞壞或意外關掉。
    2. thread 內各自 session 寫入的資料，外層 session 立刻查得到（同一個
       sqlite 檔案，commit 後對新 session 可見），代表資料確實落地，不是
       卡在某個沒 commit/沒關閉的孤兒 session 裡。
    """
    monkeypatch.setattr(usage, "collect_storage_gb", lambda serial, base=None: {"SN001": 1.5, "SN002": 2.5}[serial])

    result = await usage.run_collection(db, DAY)
    assert result["cameras"] == 2

    # 外層 session 採集後仍可正常查詢（沒有被跨執行緒的 session 搞壞）。
    rows = {r.camera_id: r for r in db.query(BillingUsageDaily).all()}
    assert rows[1].storage_gb == 1.5
    assert rows[2].storage_gb == 2.5

    # 外層 session 採集後仍可正常寫入（session 本身健康、未被關閉或弄髒）。
    extra = BillingSubscription(camera_id=99, customer_id=customer.id,
                                plan_id=subs.id, status="cancelled", camera_serial="SN099")
    db.add(extra)
    db.commit()
    db.refresh(extra)
    assert extra.id is not None


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
async def test_serial解析不到仍寫入timelapse_secs且獨立計數(db, customer, subs, monkeypatch):
    monkeypatch.setattr(usage, "collect_storage_gb", lambda serial, base=None: {"SN001": 1.5, "SN002": 2.5}[serial])
    sub2 = db.query(BillingSubscription).filter(BillingSubscription.camera_id == 2).first()
    sub2.camera_serial = None  # 模擬未快取且無法解析（env 沒設定）
    db.commit()
    db.add(TimelapsJob(user_id=customer.id, job_id="j2", camera_id=2, status="completed",
                       image_count=600, fps=30, created_at=datetime(2026, 8, 19, 10, 0)))
    db.commit()

    result = await usage.run_collection(db, DAY)

    assert result["unresolved"] == 1
    assert result["failed"] == 0
    assert result["cameras"] == 1
    rows = {r.camera_id: r for r in db.query(BillingUsageDaily).all()}
    assert 2 in rows                    # timelapse_secs 可回填，照寫入一列
    assert rows[2].timelapse_secs == 20  # 600 張 / 30fps = 20 秒
    assert rows[2].storage_gb == 0.0    # 沒有舊列可沿用，storage_gb 為 0（不嘗試採集新值）
    assert rows[1].storage_gb == 1.5    # 其他正常相機不受影響


@pytest.mark.anyio
async def test_補跑舊日期不會把新快照寫進過去那天(db, customer, subs, monkeypatch):
    """先有 08-20 的列（storage 5.0），再補跑 08-15。serial 解析不到時沿用的
    prev 必須是「08-15 或更早」的列，不能取到 08-20 的 5.0 寫進 08-15——
    那正是污染歷史（docstring 明講要避免的事）。"""
    monkeypatch.setattr(usage, "collect_storage_gb", lambda serial, base=None: {"SN001": 1.0, "SN002": 9.0}[serial])
    await usage.run_collection(db, "2026-08-20")  # 先產生 08-20 的正常列，storage 9.0

    sub2 = db.query(BillingSubscription).filter(BillingSubscription.camera_id == 2).first()
    sub2.camera_serial = None  # 模擬補跑舊日期時 serial 解析不到
    db.commit()

    result = await usage.run_collection(db, "2026-08-15")  # 補跑更早的日期

    assert result["unresolved"] == 1
    row_0815 = db.query(BillingUsageDaily).filter(
        BillingUsageDaily.camera_id == 2, BillingUsageDaily.date == "2026-08-15",
    ).first()
    assert row_0815.storage_gb == 0.0  # 沒有 08-15（或更早）的舊列可沿用，不能借用 08-20 的 9.0


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


@pytest.mark.anyio
async def test_serial連續解析不到多天_streak計數且unresolved不受upsert失敗影響(db, customer, subs, monkeypatch):
    """серial 解析不到、連續好幾天沿用同一個 storage_gb：streak 應該逐天遞增，
    超過門檻（7 天）log 要升級成 error；unresolved 只在 upsert 真的成功後才計數。"""
    monkeypatch.setattr(usage, "collect_storage_gb", lambda serial, base=None: {"SN001": 1.5, "SN002": 2.5}[serial])
    sub2 = db.query(BillingSubscription).filter(BillingSubscription.camera_id == 2).first()
    sub2.camera_serial = None
    db.commit()

    days = [f"2026-08-{d:02d}" for d in range(10, 19)]  # 9 天，超過門檻 7
    for day in days:
        await usage.run_collection(db, day)

    rows = db.query(BillingUsageDaily).filter(BillingUsageDaily.camera_id == 2).order_by(
        BillingUsageDaily.date).all()
    assert len(rows) == len(days)
    assert all(r.storage_gb == 0.0 for r in rows)  # 從未成功採集過，沿用值是 0.0

    streak_on_last_day = usage.carried_forward_streak_days(db, 2, "2026-08-19", 0.0)
    assert streak_on_last_day == len(days) + 1  # 含「2026-08-19」這筆要寫的


@pytest.mark.anyio
async def test_unresolved只在upsert成功後才計數(db, customer, subs, monkeypatch):
    """回歸：以前 unresolved += 1 發生在 upsert_usage 之前，若 upsert 拋例外，
    同一台相機會被同時計入 unresolved 與 failed，破壞 total == ok+failed+unresolved
    的不變式。"""
    monkeypatch.setattr(usage, "collect_storage_gb", lambda serial, base=None: {"SN001": 1.5}[serial])
    sub2 = db.query(BillingSubscription).filter(BillingSubscription.camera_id == 2).first()
    sub2.camera_serial = None
    db.commit()

    def _boom(*a, **kw):
        raise RuntimeError("db 掛了")
    monkeypatch.setattr(usage, "upsert_usage", _boom)

    result = await usage.run_collection(db, DAY)

    assert result["unresolved"] == 0
    assert result["failed"] == 2  # 兩台都在 upsert 這關失敗
    assert result["total"] == result["cameras"] + result["failed"] + result["unresolved"]


def test_carried_forward_streak_days純函式(db, customer, subs):
    """沒有舊列時 streak 是 1（只算今天要寫的這筆）；遇到不同值就停止往回數。"""
    assert usage.carried_forward_streak_days(db, 1, "2026-08-19", 5.0) == 1

    db.add(BillingUsageDaily(camera_id=1, date="2026-08-17", timelapse_secs=0, storage_gb=5.0))
    db.add(BillingUsageDaily(camera_id=1, date="2026-08-18", timelapse_secs=0, storage_gb=5.0))
    db.commit()
    assert usage.carried_forward_streak_days(db, 1, "2026-08-19", 5.0) == 3

    db.add(BillingUsageDaily(camera_id=1, date="2026-08-16", timelapse_secs=0, storage_gb=9.0))
    db.commit()
    assert usage.carried_forward_streak_days(db, 1, "2026-08-19", 5.0) == 3  # 08-16 值不同，停止


def test_非admin不能手動補跑採集(client, reseller, auth_headers):
    r = client.post("/billing/admin/usage/collect?date=2026-08-19", headers=auth_headers(reseller))
    assert r.status_code == 403


def test_採集端點日期格式錯誤回422(client, admin, auth_headers):
    r = client.post("/billing/admin/usage/collect?date=2026-8-19", headers=auth_headers(admin))
    assert r.status_code == 422


def test_採集端點日期不合法回422(client, admin, auth_headers):
    """通過正規表示式格式檢查、但不是合法西曆日期的字串（如 13 月、32 號）過去會
    讓 strptime 丟出未接住的 ValueError → 500；現在應攔下回 422。"""
    r = client.post("/billing/admin/usage/collect?date=2026-13-01", headers=auth_headers(admin))
    assert r.status_code == 422
    r = client.post("/billing/admin/usage/collect?date=2026-08-32", headers=auth_headers(admin))
    assert r.status_code == 422


def test_採集端點未來日期回422(client, admin, auth_headers):
    """未來日期若被接受，today 的快照會寫在未來那天，my_quotas 取本期最大 date
    時會被永久遮蔽掉真正的每日快照。"""
    r = client.post("/billing/admin/usage/collect?date=2099-01-01", headers=auth_headers(admin))
    assert r.status_code == 422


def test_採集端點昨天日期可接受(client, admin, auth_headers, monkeypatch):
    async def _fake_run_collection(db, day):
        return {"day": day, "total": 0, "cameras": 0, "failed": 0, "unresolved": 0}
    import routers.billing as billing_router_mod
    monkeypatch.setattr(billing_router_mod, "run_collection", _fake_run_collection)

    yesterday = usage.yesterday_taipei(datetime.utcnow())
    r = client.post(f"/billing/admin/usage/collect?date={yesterday}", headers=auth_headers(admin))
    assert r.status_code == 200


# ── POST /billing/admin/usage/backfill-jobs：舊縮時任務補值（completed_at／video_duration_secs）──

class _FakeBackfillResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _patch_backfill_spark(monkeypatch, by_job_id):
    """比照 tests/test_billing_usage_backfill.py 的手法：換掉 httpx.AsyncClient。

    usage.httpx 與 routers/jobs.py 的 httpx 是同一個模組物件，monkeypatch 這裡
    會同時影響 routers.jobs._query_spark_job（backfill_all_missing_job_fields
    內部沿用的那個查詢函式），不必分開 patch 兩處。
    """
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
                return _FakeBackfillResponse(404, {})
            return _FakeBackfillResponse(200, payload)

    monkeypatch.setattr(usage.httpx, "AsyncClient", _FakeClient)


def _backfill_job(db, user_id, job_id, status="completed", completed_at=None,
                  video_duration_secs=None, image_count=900, fps=30):
    j = TimelapsJob(user_id=user_id, job_id=job_id, camera_id=1, status=status,
                    image_count=image_count, fps=fps,
                    created_at=datetime(2026, 7, 10, 10, 0),
                    completed_at=completed_at, video_duration_secs=video_duration_secs)
    db.add(j); db.commit(); db.refresh(j)
    return j


@pytest.mark.anyio
async def test_補值缺兩欄位的任務都被補上(db, customer, monkeypatch):
    _backfill_job(db, customer.id, "j1")
    _patch_backfill_spark(monkeypatch, {
        "j1": {"status": "completed", "completed_at": "2026-07-10T12:00:00+08:00",
              "video_duration_secs": 42.0},
    })

    result = await usage.backfill_all_missing_job_fields(db)

    assert result["scanned"] == 1
    assert result["filled_completed_at"] == 1
    assert result["filled_duration"] == 1
    assert result["failed"] == 0
    job = db.query(TimelapsJob).filter(TimelapsJob.job_id == "j1").first()
    assert job.completed_at is not None
    assert job.video_duration_secs == 42.0


@pytest.mark.anyio
async def test_補值不覆蓋既有值只補NULL的欄位(db, customer, monkeypatch):
    existing_completed_at = datetime(2026, 7, 11, 3, 0)
    _backfill_job(db, customer.id, "j2", completed_at=existing_completed_at,
                 video_duration_secs=None)
    _patch_backfill_spark(monkeypatch, {
        "j2": {"status": "completed", "completed_at": "2026-07-20T00:00:00+08:00",
              "video_duration_secs": 99.0},
    })

    result = await usage.backfill_all_missing_job_fields(db)

    assert result["filled_completed_at"] == 0  # 已有值，不算補上，也不被覆寫
    assert result["filled_duration"] == 1
    job = db.query(TimelapsJob).filter(TimelapsJob.job_id == "j2").first()
    assert job.completed_at == existing_completed_at  # 完全沒被 Spark 的新值改掉
    assert job.video_duration_secs == 99.0


@pytest.mark.anyio
async def test_spark查不到某筆計入failed其他筆照常補(db, customer, monkeypatch):
    _backfill_job(db, customer.id, "ok")
    _backfill_job(db, customer.id, "gone")
    _patch_backfill_spark(monkeypatch, {
        "ok": {"status": "completed", "completed_at": "2026-07-10T12:00:00+08:00",
              "video_duration_secs": 10.0},
        "gone": None,  # 模擬查詢失敗（連線例外）
    })

    result = await usage.backfill_all_missing_job_fields(db)

    assert result["scanned"] == 2
    assert result["filled_completed_at"] == 1
    assert result["filled_duration"] == 1
    assert result["failed"] == 1
    assert result["failed_jobs"][0]["job_id"] == "gone"
    ok_job = db.query(TimelapsJob).filter(TimelapsJob.job_id == "ok").first()
    gone_job = db.query(TimelapsJob).filter(TimelapsJob.job_id == "gone").first()
    assert ok_job.video_duration_secs == 10.0
    assert gone_job.video_duration_secs is None
    assert gone_job.completed_at is None


@pytest.mark.anyio
async def test_補值不處理非completed任務(db, customer, monkeypatch):
    _backfill_job(db, customer.id, "processing-job", status="processing")
    called = []

    def _boom(*a, **kw):
        called.append(1)
        raise AssertionError("不該查非 completed 的任務")
    monkeypatch.setattr(usage.httpx, "AsyncClient", _boom)

    result = await usage.backfill_all_missing_job_fields(db)

    assert result["scanned"] == 0
    assert called == []


def test_補值端點非admin回403(client, reseller, auth_headers):
    r = client.post("/billing/admin/usage/backfill-jobs", headers=auth_headers(reseller))
    assert r.status_code == 403


@pytest.mark.anyio
async def test_completed_at格式無法解析時不虛構成今天(db, customer, monkeypatch):
    """parse_spark_completed_at 對解析不了的值會回退成第二個參數。補值補的是
    數週前的舊任務，若回退成 now() 會把用量靜默搬到本月、還顯示成功。"""
    _backfill_job(db, customer.id, "bad")
    _patch_backfill_spark(monkeypatch, {
        "bad": {"status": "completed", "completed_at": "not-a-timestamp",
                "video_duration_secs": None},
    })

    result = await usage.backfill_all_missing_job_fields(db)

    assert result["filled_completed_at"] == 0
    job = db.query(TimelapsJob).filter(TimelapsJob.job_id == "bad").first()
    assert job.completed_at is None
    assert any("無法解析" in f["reason"] for f in result["failed_jobs"])
