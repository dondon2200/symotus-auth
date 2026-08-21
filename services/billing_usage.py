"""每日用量採集。

資料來源都在 auth 本機，不依賴 Spark：
- 縮時秒數：timelapse_jobs 表（auth 自己的 DB），image_count / fps。
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


def job_duration_secs(image_count: Optional[int], fps: Optional[int]) -> int:
    """縮時影片長度。缺值或 fps 為 0 一律回 0——猜測會產生錯誤帳單。"""
    if not image_count or not fps:
        return 0
    return int(image_count // fps)


def bytes_to_gb(n: int) -> float:
    """位元組 → GB，保留三位小數（一天幾百 MB 的相機四捨五入到整數會全變 0）。"""
    return round(n / _GB, 3)


# ---- 以下為 I/O：資料就在 auth 自己的 DB，不必問 Spark ----

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import TimelapsJob, BillingUsageDaily, BillingSubscription


def collect_timelapse_secs(db: Session, day: str) -> dict[int, int]:
    """該日（台北）各相機完成的縮時影片總秒數。

    資料就在 auth 自己的 DB，不必問 Spark——Spark 從不回呼，狀態不可信。
    """
    start_utc, end_utc = taipei_day_bounds_utc(day)
    jobs = db.query(TimelapsJob).filter(
        TimelapsJob.status == "completed",
        TimelapsJob.created_at >= start_utc,
        TimelapsJob.created_at < end_utc,
    ).all()

    out: dict[int, int] = {}
    for j in jobs:
        if not j.camera_id:
            continue  # GDrive 來源的縮時沒有相機，不屬於任何相機的用量
        secs = job_duration_secs(j.image_count, j.fps)
        if secs == 0 and (not j.image_count or not j.fps):
            logger.info(f"billing usage: job {j.job_id} 缺 image_count/fps，計 0")
        out[j.camera_id] = out.get(j.camera_id, 0) + secs
    return out


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
    """該相機在 NAS 上佔用的空間（GB）。目錄不存在（新相機/未上傳）回 0。"""
    if not serial_id:
        return 0.0
    return bytes_to_gb(dir_size_bytes(os.path.join(base, serial_id)))


# ---- 每日採集流程：串起縮時秒數與 NAS 儲存，寫入 BillingUsageDaily ----

# 採集用的 Camera Backend 帳號：解析 camera_id → device_serial_id 需要一個
# 有全部相機可見度的帳號。未設定時跳過解析（已快取 serial 的相機仍可採集）。
COLLECTOR_CAMERA_EMAIL = os.environ.get("BILLING_COLLECTOR_CAMERA_EMAIL", "")
CAMERA_BACKEND_URL = "https://user.symotus.com"
CAMERA_SERVICE_KEY = os.environ.get("CAMERA_SERVICE_KEY", "")


async def resolve_camera_serial(db: Session, sub: BillingSubscription) -> str:
    """取得相機的 NAS 目錄名，並快取在訂閱列上。

    已快取就直接回傳——每日採集不該為每台相機重複打 Camera Backend。
    """
    if sub.camera_serial:
        return sub.camera_serial
    if not COLLECTOR_CAMERA_EMAIL or not CAMERA_SERVICE_KEY:
        logger.warning("billing usage: 未設定 BILLING_COLLECTOR_CAMERA_EMAIL/CAMERA_SERVICE_KEY，無法解析 serial")
        return ""

    async with httpx.AsyncClient(timeout=15) as client:
        tok = await client.post(
            f"{CAMERA_BACKEND_URL}/internal/auth/token",
            headers={"x-service-key": CAMERA_SERVICE_KEY},
            json={"user_id": 0, "email": COLLECTOR_CAMERA_EMAIL, "role": "admin"},
        )
        if tok.status_code != 200:
            return ""
        token = tok.json().get("access_token", "")
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


async def run_collection(db: Session, day: str) -> dict:
    """採集指定日期（台北）的用量。回 {"day", "cameras", "failed"}。

    單台相機失敗只記 log 並跳過——一台相機的 NAS 掛載問題不該讓當日整批採集消失。
    """
    secs_by_camera = collect_timelapse_secs(db, day)
    subs = db.query(BillingSubscription).filter(BillingSubscription.status == "active").all()

    ok, failed = 0, 0
    for sub in subs:
        try:
            serial = await resolve_camera_serial(db, sub)
            # 阻塞式 I/O 必須丟到 thread，否則會卡住整個 auth 的事件迴圈
            gb = await asyncio.to_thread(collect_storage_gb, serial)
            upsert_usage(db, sub.camera_id, day, secs_by_camera.get(sub.camera_id, 0), gb)
            ok += 1
        except Exception as e:
            failed += 1
            logger.warning(f"billing usage: 相機 {sub.camera_id} 採集失敗（{day}）：{e}")

    logger.info(f"billing usage: {day} 採集完成，成功 {ok} 台、失敗 {failed} 台")
    return {"day": day, "cameras": ok, "failed": failed}
