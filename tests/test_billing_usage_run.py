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
