"""每日用量採集。

資料來源都在 auth 本機，不依賴 Spark（除了縮時秒數的補值安全網——見
`backfill_missing_video_duration_secs`）：
- 縮時秒數：timelapse_jobs 表（auth 自己的 DB）的 video_duration_secs（Spark 回報的
  產出影片實際長度）；缺值時才退回 image_count / fps 的高估值估算。
- 儲存 GB：NAS 檔案系統 /homes/firmness/{serial_id}（routers/cameras.py:1330 已有先例）。

本檔前半是純函式（可完整單元測試），後半是 I/O。
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

import httpx

from services.billing_calc import TAIPEI_OFFSET

logger = logging.getLogger(__name__)

_GB = 1024 ** 3


def taipei_day_bounds_utc(day: str) -> tuple[datetime, datetime]:
    """台北時區的某一天（YYYY-MM-DD）對應的 naive UTC 區間 [起, 迄)。

    DB 存 naive UTC，但帳務的「哪一天」是台北時間，直接對 created_at 取 .date()
    會讓台北 00:00-08:00 的資料算到前一天。
    """
    start_taipei = datetime.strptime(day, "%Y-%m-%d")
    start_utc = start_taipei - TAIPEI_OFFSET
    return start_utc, start_utc + timedelta(days=1)


def yesterday_taipei(now_utc: datetime) -> str:
    """以台北時區判定的「昨天」（YYYY-MM-DD）。"""
    return (now_utc + TAIPEI_OFFSET - timedelta(days=1)).strftime("%Y-%m-%d")


def job_duration_secs(
    video_duration_secs: Optional[float],
    image_count: Optional[int],
    fps: Optional[int],
) -> int:
    """縮時影片長度（秒，無條件捨去）。

    優先用 Spark 回報的 video_duration_secs——那是產出影片的真實長度。
    image_count/fps 只是後備：image_count 是「可用的來源照片張數」，Spark 依
    target_duration_secs 抽樣，實測兩者差 2～6 倍且倍率隨任務變動，用它計費會超收。
    舊資料（欄位新增前）沒有 video_duration_secs，只能用這個高估值，並記 log。

    注意用 `is None` 判斷：0 秒是合法結果（來源只有 1 張照片），
    用 falsy 判斷會讓它退回 image_count/fps 而錯記成好幾百秒。

    對 video_duration_secs 的型別不設防：Spark 目前實測回 float，但若哪天回
    字串（如 "125.4"）等非數值型別，直接 `int()` 會拋 ValueError 讓呼叫端
    （collect_timelapse_secs）整批中斷，不只這一筆。這裡改用 `float()` 轉一手，
    轉換失敗就記 log 退回 image_count/fps。另外用 `max(0, ...)` 擋負值——
    Spark 不該回負數，但這是零成本保險，避免 int(-1.5) == -1 這種怪值進計費。
    """
    if video_duration_secs is not None:
        try:
            return max(0, int(float(video_duration_secs)))
        except (TypeError, ValueError):
            logger.warning(
                "billing usage: video_duration_secs=%r 無法轉為數值，退回 image_count/fps 估算",
                video_duration_secs,
            )
    if not image_count or not fps:
        return 0
    return max(0, int(image_count // fps))


def bytes_to_gb(n: int) -> float:
    """位元組 → GB，保留三位小數（一天幾百 MB 的相機四捨五入到整數會全變 0）。"""
    return round(n / _GB, 3)


# ---- 以下為 I/O：資料就在 auth 自己的 DB，不必問 Spark ----

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import TimelapsJob, BillingUsageDaily, BillingSubscription


def collect_timelapse_secs(db: Session, day: str) -> dict[int, int]:
    """該日（台北）各相機完成的縮時影片總秒數。

    資料就在 auth 自己的 DB，不必問 Spark——Spark 從不回呼，狀態不可信。

    以「完成日」（completed_at）而非「建立日」（created_at）歸日：任務可能跨日
    完成（例如台北 23:40 建立、隔天 04:00 才完成的任務），用建立日切會讓完成當天
    的用量永久漏計。completed_at 是後加欄位，正式庫既有資料一律是 NULL，因此用
    COALESCE(completed_at, created_at) 退回 created_at，否則既有的縮時用量會整批消失。
    """
    start_utc, end_utc = taipei_day_bounds_utc(day)
    effective_time = func.coalesce(TimelapsJob.completed_at, TimelapsJob.created_at)
    jobs = db.query(TimelapsJob).filter(
        TimelapsJob.status == "completed",
        effective_time >= start_utc,
        effective_time < end_utc,
    ).all()

    out: dict[int, int] = {}
    missing_job_ids: list[str] = []
    for j in jobs:
        if not j.camera_id:
            continue  # GDrive 來源的縮時沒有相機，不屬於任何相機的用量
        if j.video_duration_secs is None:
            missing_job_ids.append(j.job_id)
        secs = job_duration_secs(j.video_duration_secs, j.image_count, j.fps)
        out[j.camera_id] = out.get(j.camera_id, 0) + secs
    if missing_job_ids:
        # 彙總一則而非逐筆 logger.info：舊資料整批補跑（或補值安全網沒接到）
        # 那天可能有幾十筆缺值，逐筆記錄會刷版，看不出全貌。
        logger.info(
            "billing usage: %d 筆缺 video_duration_secs，退回 image_count/fps 估算"
            "（可能高估），前幾筆 job_id=%s",
            len(missing_job_ids), missing_job_ids[:5],
        )
    return out


# ── 補值安全網：collect_timelapse_secs 要用的那天，把缺 video_duration_secs 的
# completed job 逐一問 Spark 補回真值。獨立成 async 函式而非把 collect_timelapse_secs
# 本身改成 async：collect_timelapse_secs 是純 DB 查詢的同步函式，被多處（含測試）
# 直接呼叫；把 I/O 拆到呼叫端（run_collection）先跑一輪、寫回 DB 後再呼叫
# collect_timelapse_secs，改動範圍最小，也維持 collect_timelapse_secs 本身可同步測試。
#
# 併發模式比照 routers/jobs.py 的 _sync_jobs_with_spark：共用其 _query_spark_job
# （已封裝 httpx 與錯誤處理）+ Semaphore，單筆失敗（逾時/查無/Spark 錯誤）不中斷整批，
# 沿用 collect_timelapse_secs 既有的 image_count/fps fallback。
from config import settings

SPARK_API_URL = settings.SPARK_API_URL
SPARK_API_KEY = settings.SPARK_API_KEY
BACKFILL_CONCURRENCY = 4


def _spark_job_fetcher():
    """延遲匯入 routers/jobs 的單筆 Spark 查詢，供本檔兩個補值函式共用。

    在函式內才 import（而非檔案頂層）：避免與 routers/jobs.py 之間在模組載入順序上
    產生循環匯入風險（routers/jobs.py 本身頗重，牽動 GDrive 相關的一大段程式碼）。
    兩個補值函式都改用它而不是各自維護一份 httpx 呼叫，是本輪重構的重點——
    沿用同一個 SPARK_SYNC_TIMEOUT、同一套「查不到／逾時/回錯一律回 None」語意，
    不要讓兩份 Spark 呼叫邏輯各自漂移。
    """
    from routers.jobs import _query_spark_job, parse_spark_completed_at
    return _query_spark_job, parse_spark_completed_at


async def backfill_missing_video_duration_secs(db: Session, day: str) -> int:
    """把該日（台北）completed 且缺 video_duration_secs 的相機縮時 job 向 Spark 補值並寫回 DB。

    只查缺值的（每日 completed 縮時任務個位數到數十筆，量級可控）；查得到就
    直接寫回 DB 持久化，讓同日重跑與後續報表一致；查不到/逾時/Spark 回錯
    的單筆不中斷整批，靜默略過，collect_timelapse_secs 會沿用 image_count/fps。

    回傳成功補值的筆數（供呼叫端記 log/回應用，非必要）。
    """
    start_utc, end_utc = taipei_day_bounds_utc(day)
    effective_time = func.coalesce(TimelapsJob.completed_at, TimelapsJob.created_at)
    jobs = db.query(TimelapsJob).filter(
        TimelapsJob.status == "completed",
        TimelapsJob.video_duration_secs.is_(None),
        effective_time >= start_utc,
        effective_time < end_utc,
    ).all()
    if not jobs:
        return 0

    query_spark_job, _ = _spark_job_fetcher()
    sem = asyncio.Semaphore(BACKFILL_CONCURRENCY)

    async def fetch(job):
        async with sem:
            data = await query_spark_job(job.job_id)
            if data:
                val = data.get("video_duration_secs")
                if val is not None:
                    return job, val
            return job, None

    results = await asyncio.gather(*[fetch(j) for j in jobs])

    filled = 0
    for job, val in results:
        if val is not None:
            job.video_duration_secs = val
            filled += 1
    if filled:
        db.commit()

    missing = len(jobs) - filled
    if missing:
        logger.info(
            "billing usage: %s 補值 %d/%d 筆成功，剩 %d 筆維持 image_count/fps fallback",
            day, filled, len(jobs), missing,
        )
    return filled


async def backfill_all_missing_job_fields(db: Session, limit: int = 500) -> dict:
    """一次性、跨全表的維運補值：把所有 completed 但缺 completed_at 或
    video_duration_secs 的縮時任務向 Spark 補齊。

    與 backfill_missing_video_duration_secs 的定位不同：那個是「當天採集前」的
    例行安全網，只掃當天；這個是計費上線後對既有舊資料的一次性維運操作，掃
    全表，且兩個欄位一起補（completed_at 也是後加欄位，舊資料全部是 NULL）。
    兩者共用 `_spark_job_fetcher()` 取得的單筆查詢，不重複維護 Spark 呼叫邏輯。

    冪等且安全：
    - 只補目前是 NULL 的欄位，**絕不覆蓋既有值**——completed_at 一旦有值就不再碰，
      避免用量的歸日結果被悄悄搬到別的月份。
    - 單筆查不到／逾時／回錯，或 Spark 回應裡剛好沒有這個任務缺的那個欄位，
      都不中斷整批，計入 failed 並記原因，其餘任務照常處理。
    - `limit` 防止一次撈太多筆同時打 Spark（目前實際只有 18 筆，遠低於預設 500）；
      併發用既有的 BACKFILL_CONCURRENCY（4 路），18 筆最壞情況也只要幾輪
      SPARK_SYNC_TIMEOUT（6 秒），不需要另外做批次分頁或背景排程。

    回傳 {"scanned", "filled_completed_at", "filled_duration", "failed", "failed_jobs"}，
    failed_jobs 是 [{"job_id", "reason"}, ...]，供維運判讀「補值後為什麼還有缺值」。
    """
    query_spark_job, parse_spark_completed_at = _spark_job_fetcher()

    jobs = db.query(TimelapsJob).filter(
        TimelapsJob.status == "completed",
        (TimelapsJob.completed_at.is_(None)) | (TimelapsJob.video_duration_secs.is_(None)),
    ).order_by(TimelapsJob.id).limit(limit).all()

    scanned = len(jobs)
    if not jobs:
        return {"scanned": 0, "filled_completed_at": 0, "filled_duration": 0,
                "failed": 0, "failed_jobs": []}

    sem = asyncio.Semaphore(BACKFILL_CONCURRENCY)

    async def fetch(job):
        async with sem:
            return job, await query_spark_job(job.job_id)

    results = await asyncio.gather(*[fetch(j) for j in jobs])

    filled_completed_at = 0
    filled_duration = 0
    failed_jobs: list[dict] = []
    for job, data in results:
        if data is None:
            # 查不到／逾時／回錯：Spark 端這筆任務本身無法查證，計入 failed，
            # 不影響其他筆——這是一次性維運操作，單筆失敗不該讓整批中止。
            failed_jobs.append({"job_id": job.job_id, "reason": "Spark 查無此任務／逾時／回應錯誤"})
            continue

        got_something = False
        if job.completed_at is None:
            raw = data.get("completed_at")
            # parse_spark_completed_at 對「非空但解析不出來」的值會回退成第二個
            # 參數。在 PUT /jobs 那邊 now() 是合理估計（任務剛完成），但這裡補的是
            # 數週前的舊任務，回退成 now() 會把用量靜默搬到本月、還顯示成功。改用
            # 哨兵值辨識解析失敗，失敗就當作沒補到。
            # 哨兵要用未來時間：函式內部會 min(parsed, 第二參數) 夾制，用過去
            # 的哨兵會把有效結果一起夾成哨兵。
            sentinel = datetime(9999, 12, 31)
            parsed = parse_spark_completed_at(raw, sentinel) if raw else sentinel
            if parsed != sentinel:
                # 仍保留原本的未來時間夾制。
                job.completed_at = min(parsed, datetime.utcnow())
                filled_completed_at += 1
                got_something = True
            elif raw:
                failed_jobs.append({
                    "job_id": job.job_id,
                    "reason": f"Spark completed_at 格式無法解析：{raw!r}",
                })
            # raw 為空：Spark 回應裡沒帶完成時間，不虛構成 now()（那會讓用量
            # 歸到錯誤的月份），維持 NULL，等下次重跑或人工排查。
        if job.video_duration_secs is None:
            val = data.get("video_duration_secs")
            if val is not None:
                job.video_duration_secs = val
                filled_duration += 1
                got_something = True
        if not got_something:
            failed_jobs.append({
                "job_id": job.job_id,
                "reason": "Spark 查得到任務，但回應未帶缺失欄位的值",
            })

    if filled_completed_at or filled_duration:
        db.commit()

    logger.info(
        "billing usage: 舊任務補值完成，掃描 %d 筆，補上 completed_at %d 筆、"
        "video_duration_secs %d 筆，%d 筆失敗",
        scanned, filled_completed_at, filled_duration, len(failed_jobs),
    )
    return {
        "scanned": scanned,
        "filled_completed_at": filled_completed_at,
        "filled_duration": filled_duration,
        "failed": len(failed_jobs),
        "failed_jobs": failed_jobs,
    }


def upsert_usage(db: Session, camera_id: int, day: str, timelapse_secs: int, storage_gb: float) -> None:
    """寫入該相機該日用量。已存在就覆蓋——採集失敗後補跑不會重複累加。"""
    row = db.query(BillingUsageDaily).filter(
        BillingUsageDaily.camera_id == camera_id,
        BillingUsageDaily.date == day,
    ).first()
    if row:
        row.timelapse_secs = timelapse_secs
        row.storage_gb = storage_gb
        row.collected_at = datetime.utcnow()
        db.commit()
        return
    db.add(BillingUsageDaily(
        camera_id=camera_id, date=day,
        timelapse_secs=timelapse_secs, storage_gb=storage_gb,
        collected_at=datetime.utcnow(),
    ))
    try:
        db.commit()
    except IntegrityError:
        # 先查後寫沒有併發防護：採集逾時被重試、或每日排程與手動補跑
        # （POST /billing/admin/usage/collect）重疊時，兩個執行緒都可能查不到
        # 既有列而各自 insert，第二個 commit 會撞上 UNIQUE(camera_id, date)。
        # 做法比照 routers/billing.py 的 get_or_create_customer：
        # rollback 後重查，這裡語意是覆蓋，所以把重查到的列更新成本次要寫的值。
        db.rollback()
        row = db.query(BillingUsageDaily).filter(
            BillingUsageDaily.camera_id == camera_id,
            BillingUsageDaily.date == day,
        ).first()
        if row is None:
            # 理論上不該發生（撞到 UNIQUE 代表該列一定存在）；
            # 若真的查不到，代表狀況超出預期，往上拋不要吞掉——
            # 這個專案很在意「失敗不可回報成功」。
            raise
        row.timelapse_secs = timelapse_secs
        row.storage_gb = storage_gb
        row.collected_at = datetime.utcnow()
        db.commit()


# NAS 根目錄。與 routers/cameras.py:1330 的 /homes/firmness/{serial_id} 一致。
NAS_BASE = "/homes/firmness"


def dir_size_bytes(path: str) -> int:
    """走訪目錄加總檔案大小。

    阻塞式 I/O——呼叫端必須用 asyncio.to_thread 包起來，否則會卡住整個事件迴圈。
    auth service 同時是所有相機 CRUD 的代理，若在事件迴圈裡直接呼叫本函式，
    走訪大目錄會卡住全站。
    """
    if not os.path.isdir(path):
        return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                # 採集期間檔案被刪或權限問題：跳過單一檔案，不讓整台相機的統計失敗
                continue
    return total


def collect_storage_gb(serial_id: str, base: str = NAS_BASE) -> float:
    """該相機在 NAS 上目前的總佔用空間（GB）——時間點快照，不是當日的增量。
    目錄不存在（新相機/未上傳）回 0。呼叫端寫入 BillingUsageDaily.storage_gb 時
    不可對多天加總，期間用量須取期間內最後一列。"""
    if not serial_id:
        return 0.0
    return bytes_to_gb(dir_size_bytes(os.path.join(base, serial_id)))


# ---- 每日採集流程：串起縮時秒數與 NAS 儲存，寫入 BillingUsageDaily ----

# 採集用的 Camera Backend 帳號：解析 camera_id → device_serial_id 需要一個
# 有全部相機可見度的帳號。未設定時跳過解析（已快取 serial 的相機仍可採集）。
COLLECTOR_CAMERA_EMAIL = os.environ.get("BILLING_COLLECTOR_CAMERA_EMAIL", "")
CAMERA_BACKEND_URL = "https://user.symotus.com"
CAMERA_SERVICE_KEY = os.environ.get("CAMERA_SERVICE_KEY", "")

if not COLLECTOR_CAMERA_EMAIL or not CAMERA_SERVICE_KEY:
    # error 而非 warning：這條路徑最系統性——一旦漏設，所有未快取 serial 的
    # 相機（尤其新相機）當天的 NAS 儲存用量都會無法採集。放在每台相機的迴圈
    # 裡刷 warning 一次採集會刷 N 則反而更容易被忽略，所以在 import 時只講一次。
    logger.error(
        "billing usage: 未設定 BILLING_COLLECTOR_CAMERA_EMAIL/CAMERA_SERVICE_KEY，"
        "新相機（尚未快取 camera_serial）的 NAS 儲存用量將無法採集"
    )


async def fetch_collector_token(timeout: float = 15) -> str:
    """取得採集用的 Camera Backend token。

    每日採集只需要拿一次——呼叫端（run_collection）應在迴圈外呼叫本函式一次，
    而不是讓 resolve_camera_serial 每台相機各自重打 /internal/auth/token。
    """
    if not COLLECTOR_CAMERA_EMAIL or not CAMERA_SERVICE_KEY:
        return ""
    async with httpx.AsyncClient(timeout=timeout) as client:
        tok = await client.post(
            f"{CAMERA_BACKEND_URL}/internal/auth/token",
            headers={"x-service-key": CAMERA_SERVICE_KEY},
            json={"user_id": 0, "email": COLLECTOR_CAMERA_EMAIL, "role": "admin"},
        )
        if tok.status_code != 200:
            return ""
        return tok.json().get("access_token", "")


async def resolve_camera_serial(db: Session, sub: BillingSubscription, token: str = "") -> str:
    """取得相機的 NAS 目錄名，並快取在訂閱列上。

    已快取就直接回傳——每日採集不該為每台相機重複打 Camera Backend。
    `token` 由呼叫端（run_collection）先取好傳入，避免每台相機各自 mint 一次。
    """
    if sub.camera_serial:
        return sub.camera_serial
    if not COLLECTOR_CAMERA_EMAIL or not CAMERA_SERVICE_KEY:
        logger.warning("billing usage: 未設定 BILLING_COLLECTOR_CAMERA_EMAIL/CAMERA_SERVICE_KEY，無法解析 serial")
        return ""
    if not token:
        return ""

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{CAMERA_BACKEND_URL}/api/cameras/{sub.camera_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code != 200:
            return ""
        data = r.json()
        basic = data.get("basic_info", data)
        serial = basic.get("device_serial_id") or basic.get("serial_id") or basic.get("serial") or ""

    if serial:
        sub.camera_serial = serial
        db.commit()
    return serial


# serial 解析不到、storage_gb 連續沿用超過這麼多天就把 log 從 warning 升級成
# error（多半代表相機已從 Camera Backend 刪除，NAS 目錄早就不在了，卻還在
# 每天重寫舊快照）。
CARRY_FORWARD_ALERT_DAYS = 7


def carried_forward_streak_days(db: Session, camera_id: int, day: str, carried_value: float) -> int:
    """回推連續幾天（含今天要寫的這筆）storage_gb 都是同一個沿用值。

    沒有明確的「這列是沿用寫入的」欄位，用「storage_gb 剛好等於沿用值」當代理：
    一旦遇到不同的值（代表那天曾經真的採集成功）就停止往回數。
    """
    streak = 1
    rows = db.query(BillingUsageDaily).filter(
        BillingUsageDaily.camera_id == camera_id,
        BillingUsageDaily.date < day,
    ).order_by(BillingUsageDaily.date.desc()).all()
    for row in rows:
        if row.storage_gb == carried_value:
            streak += 1
        else:
            break
    return streak


async def run_collection(db: Session, day: str) -> dict:
    """採集指定日期（台北）的用量。

    回傳 {"day", "total", "cameras", "unresolved", "failed"}：
    - total：當日應採集的訂閱數（分母）
    - cameras：storage 與 timelapse 都完整採集成功的台數（unresolved 分支雖然也會
      寫入一列，但只有 timelapse_secs 是新採的，storage_gb 是沿用舊值，不計入這裡）
    - unresolved：serial 解析不到而跳過的台數（env 沒設定，或相機已不存在）——
      這是設定問題，不是採集失敗，維運動作不同所以獨立計數，不併入 failed。
    - failed：serial 解析成功但採集過程本身出錯（NAS 掛載問題等）的台數

    單台相機失敗只記 log 並跳過——一台相機的 NAS 掛載問題不該讓當日整批採集消失。

    serial 解析不到時，timelapse_secs 與 storage_gb 處理方式不同：
    - timelapse_secs 來自 auth 自己的 DB，跟 serial 解析無關，事後可安全回填，
      所以照寫，不因為 serial 問題連累原本查得到的資料。
    - storage_gb 是時間點快照，寫 0（或亂寫）之後即使補設定重跑，也只能拿到
      「重跑當下」的目錄大小，不是當天的，等同永久污染歷史；所以沿用該相機
      目前列裡的舊值（沒有列過就是 0；也就是說從未採集成功過的相機，這裡寫的
      不是「沿用」而是貨真價實的 0.0），不嘗試採集新值。
    unresolved 計數不受影響，仍然照計，代表這台相機當天的 storage_gb 沒有更新。
    """
    # 補值安全網先跑：把當天缺 video_duration_secs 的 completed job 向 Spark
    # 補回真值並寫回 DB，collect_timelapse_secs 才能吃到補好的值（同一個 db
    # session，補值的 commit 對接下來的查詢立即可見）。
    await backfill_missing_video_duration_secs(db, day)
    secs_by_camera = collect_timelapse_secs(db, day)
    subs = db.query(BillingSubscription).filter(BillingSubscription.status == "active").all()

    # 迴圈外先取一次 token：首次上線時所有訂閱的 camera_serial 都是 NULL，
    # 若在 resolve_camera_serial 內個別 mint，會對 Camera Backend 發 2N 次請求。
    token = await fetch_collector_token()

    ok, failed, unresolved = 0, 0, 0
    for sub in subs:
        try:
            serial = await resolve_camera_serial(db, sub, token)
            if not serial:
                # storage_gb 沿用該相機既有列的舊值（沒有就是 0）——timelapse_secs
                # 仍照寫，不因為 serial 解析不到就連帶漏掉可回填的資料。
                # 必須限制 date <= day：補跑舊日期（POST .../collect?date=較早日期）時，
                # 若不加這個條件，會取到「之後某天」的快照寫回這個較早的日期，
                # 那正是本函式 docstring 說要避免的「永久污染歷史」。
                prev = db.query(BillingUsageDaily).filter(
                    BillingUsageDaily.camera_id == sub.camera_id,
                    BillingUsageDaily.date <= day,
                ).order_by(BillingUsageDaily.date.desc()).first()
                prev_gb = prev.storage_gb if prev else 0.0
                # 相機可能已從 Camera Backend 刪除（resolve 404），這種情況下沿用
                # 舊值會被每天重寫、永遠不會停——追蹤連續沿用了幾天，超過門檻就
                # 升級成 error 讓人注意到（例如該補訂閱下架而非繼續累計用量）。
                streak = carried_forward_streak_days(db, sub.camera_id, day, prev_gb)
                msg = (
                    f"billing usage: 相機 {sub.camera_id} 無法解析 serial（{day}），"
                    f"storage_gb 沿用舊值 {prev_gb}，已連續沿用 {streak} 天，timelapse_secs 照常寫入"
                )
                if streak > CARRY_FORWARD_ALERT_DAYS:
                    logger.error(msg)
                else:
                    logger.warning(msg)
                upsert_usage(db, sub.camera_id, day, secs_by_camera.get(sub.camera_id, 0), prev_gb)
                unresolved += 1
                continue
            # 阻塞式 I/O 必須丟到 thread，否則會卡住整個 auth 的事件迴圈
            gb = await asyncio.to_thread(collect_storage_gb, serial)
            upsert_usage(db, sub.camera_id, day, secs_by_camera.get(sub.camera_id, 0), gb)
            ok += 1
        except Exception as e:
            failed += 1
            logger.warning(f"billing usage: 相機 {sub.camera_id} 採集失敗（{day}）：{e}")

    logger.info(f"billing usage: {day} 採集完成，成功 {ok} 台、失敗 {failed} 台、無法解析 {unresolved} 台")
    return {"day": day, "total": len(subs), "cameras": ok, "failed": failed, "unresolved": unresolved}


# 每日採集時間（台北時區的小時）。03:00 是離峰，且前一天的資料已全部落地。
COLLECT_HOUR_TAIPEI = 3


def seconds_until_next_collect(now_utc: datetime) -> float:
    """算出距離下一個台北時間 03:00 還有幾秒（純函式，可單元測試）。

    抽成獨立函式是為了讓跨日、跨時區的邊界可測——排程迴圈本身有無限迴圈
    + 長時間 sleep，無法直接測試。
    """
    taipei = now_utc + TAIPEI_OFFSET
    target = taipei.replace(hour=COLLECT_HOUR_TAIPEI, minute=0, second=0, microsecond=0)
    if target <= taipei:
        target += timedelta(days=1)
    return (target - taipei).total_seconds()


async def start_usage_collector() -> None:
    """每日在台北時間 03:00 採集前一天的用量。

    比照 services/camera_notifier.py 的模式，由 main.py 的 startup 以
    asyncio.create_task 啟動。整個迴圈包在 try 裡——採集出錯絕不能讓服務停掉
    （auth 同時是所有相機 CRUD 的代理）。asyncio.CancelledError 例外：那是
    正常的關閉流程，往上拋才能讓服務乾淨關閉，吞掉會卡住 shutdown。

    限制：storage_gb 是時間點快照，無法回溯補算——排程漏跑一天之後再補跑，
    寫進去的是「補跑當下」的 NAS 目錄大小，不是那天的。所以排程的可靠性
    比補跑機制更重要，這也是本函式把每一步失敗都設計成「記 log 後重試」
    而不是「放棄」的原因。
    """
    from database import SessionLocal

    logger.info("billing usage collector 已啟動")
    while True:
        try:
            await asyncio.sleep(seconds_until_next_collect(datetime.utcnow()))

            db = SessionLocal()
            try:
                day = yesterday_taipei(datetime.utcnow())
                await run_collection(db, day)
            finally:
                db.close()
        except asyncio.CancelledError:
            logger.info("billing usage collector 已停止")
            raise
        except Exception as e:
            logger.error(f"billing usage collector 發生錯誤，60 秒後重試：{e}")
            await asyncio.sleep(60)
