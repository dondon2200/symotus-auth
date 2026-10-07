"""計費 v2 核心：帳單產生排程、相機鎖定狀態。

規格：symotus-frontend/docs/superpowers/specs/2026-10-07-camera-subscription-billing-design.md §6–§8

邊界：本模組只「讀」User/CameraAccess；auth 既有程式碼只透過 assert_camera_unlocked /
get_lock_map / camera_billing_flags / locked_serials 這幾個入口使用它。
"""
import asyncio
import logging
import time as _time
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import BillingBill, BillingSubscriptionV2, User
from services.billing_dates import (
    TAIPEI, bill_date, due_date_of, taipei_date_to_utc_naive, taipei_today, term_end_of,
)

logger = logging.getLogger(__name__)

OPEN_STATUSES = ("scheduled", "active", "suspended")
MAX_NEW_BILLS_PER_RUN = 24  # 超過視為資料錯誤（例如起始日打成 2016 年），不開（spec §7）


# ── 帳單產生 ─────────────────────────────────────────────────────────

def _end(sub: BillingSubscriptionV2, on: date, reason: str) -> None:
    sub.status = "ended"
    sub.ended_at = taipei_date_to_utc_naive(on)
    sub.end_reason = reason
    sub.cancel_at = None


def advance_subscription(db: Session, sub: BillingSubscriptionV2, today: date) -> int:
    """把一份訂閱推進到 today：轉態、續約、補開所有帳單日 ≤ today 的帳單。回傳新開張數。

    冪等：已存在的期別跳過。呼叫端負責 commit 與並發衝突（UNIQUE 撞到時整份 rollback）。"""
    if sub.status == "scheduled":
        if sub.start_date > today:
            return 0
        sub.status = "active"
    if sub.status not in ("active", "suspended"):
        return 0

    existing = {
        r[0] for r in db.query(BillingBill.period_start)
        .filter(BillingBill.subscription_id == sub.id).all()
    }
    anchor = sub.anchor_day
    created = 0
    k = 0
    while True:
        bd = bill_date(sub.start_date, k, sub.cycle, anchor)
        if bd > today:
            break
        if sub.cancel_at and bd >= sub.cancel_at:
            _end(sub, sub.cancel_at, "not_renewed")
            break
        if sub.term == "annual":
            tend = term_end_of(sub.term_start, anchor)
            if bd >= tend:
                if sub.auto_renew:
                    sub.term_start = tend
                else:
                    _end(sub, tend, "not_renewed")
                    break
        if bd not in existing:
            if created >= MAX_NEW_BILLS_PER_RUN:
                logger.error("billing: 訂閱 #%s 一次需補開超過 %s 期，視為資料錯誤，停止補開",
                             sub.id, MAX_NEW_BILLS_PER_RUN)
                break
            db.add(BillingBill(
                subscription_id=sub.id,
                camera_id=sub.camera_id,
                customer_id=sub.customer_id,
                camera_name=sub.camera_name,
                plan_name=sub.plan_name,
                cycle=sub.cycle,
                period_start=bd,
                period_end=bill_date(sub.start_date, k + 1, sub.cycle, anchor),
                due_date=due_date_of(bd, anchor),
                amount=sub.price,
                status="unpaid",
            ))
            existing.add(bd)
            created += 1
        k += 1
    return created


def advance_one(db: Session, sub: BillingSubscriptionV2, today: Optional[date] = None) -> int:
    """單份訂閱推進並 commit（建立訂閱、恢復服務時用）。並發撞 UNIQUE 時視為別人已開好。"""
    today = today or taipei_today()
    try:
        n = advance_subscription(db, sub, today)
        db.commit()
    except IntegrityError:
        db.rollback()
        logger.info("billing: 訂閱 #%s 帳單已由並發作業開立", sub.id)
        n = 0
    invalidate_lock_cache()
    return n


def run_billing_cycle(today: Optional[date] = None) -> dict:
    """推進所有未結束的訂閱。單份失敗只記 log、不中斷整批。各自開 session（會被丟進 thread）。"""
    from database import SessionLocal

    today = today or taipei_today()
    stats = {"subscriptions": 0, "bills_created": 0, "failed": 0}
    with SessionLocal() as db:
        ids = [r[0] for r in db.query(BillingSubscriptionV2.id)
               .filter(BillingSubscriptionV2.status.in_(OPEN_STATUSES)).all()]
    for sid in ids:
        with SessionLocal() as db:
            sub = db.query(BillingSubscriptionV2).filter(BillingSubscriptionV2.id == sid).first()
            if not sub:
                continue
            try:
                stats["bills_created"] += advance_subscription(db, sub, today)
                db.commit()
                stats["subscriptions"] += 1
            except Exception as e:  # noqa: BLE001 — 單份失敗不可拖垮整批
                db.rollback()
                stats["failed"] += 1
                logger.warning("billing: 推進訂閱 #%s 失敗：%s", sid, e)
    invalidate_lock_cache()
    logger.info("billing: 帳單排程完成 %s（台北 %s）", stats, today)
    return stats


def _seconds_until_next_run(now: Optional[datetime] = None) -> float:
    """距離下一個台北 00:10 的秒數。"""
    now = (now or datetime.now(TAIPEI)).astimezone(TAIPEI)
    target = now.replace(hour=0, minute=10, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def start_billing_scheduler():
    """背景迴圈：啟動時先補跑一次，之後每天台北 00:10。DB 工作丟進 thread，不卡事件迴圈。"""
    while True:
        try:
            await asyncio.to_thread(run_billing_cycle)
        except Exception as e:  # noqa: BLE001
            logger.error("billing: 帳單排程失敗：%s", e)
            await asyncio.sleep(60)
            continue
        await asyncio.sleep(_seconds_until_next_run())


# ── 鎖定狀態 ─────────────────────────────────────────────────────────

_LOCK_TTL = 10.0
_lock_cache: dict = {"at": 0.0, "map": None}


def _safe_rollback(db) -> None:
    try:
        db.rollback()
    except Exception:  # noqa: BLE001
        pass


def invalidate_lock_cache() -> None:
    _lock_cache["at"] = 0.0
    _lock_cache["map"] = None


def _taipei_date_str(ts: Optional[datetime]) -> Optional[str]:
    return taipei_today(ts).isoformat() if ts else None


def get_lock_map(db: Session) -> dict[int, dict]:
    """{camera_id: {"state": "suspended"|"ended", "since": "YYYY-MM-DD", "serial": str|None}}。

    每台相機只看最新一份訂閱（id 最大）：重新開通（新訂閱）就立即解鎖（spec §13 S12b）。
    10 秒程序內快取；處置類 API 寫入時主動清除。"""
    now = _time.monotonic()
    if _lock_cache["map"] is not None and now - _lock_cache["at"] < _LOCK_TTL:
        return _lock_cache["map"]
    latest: dict[int, BillingSubscriptionV2] = {}
    try:
        subs = db.query(BillingSubscriptionV2).order_by(BillingSubscriptionV2.id).all()
    except Exception as e:  # noqa: BLE001
        # 計費查詢失敗時放行（不鎖），不讓計費故障拖垮所有相機端點；大聲記 log。
        _safe_rollback(db)
        logger.error("billing: 讀取鎖定狀態失敗，暫時視為全部未鎖定：%s", e)
        return {}
    for sub in subs:
        latest[sub.camera_id] = sub
    result: dict[int, dict] = {}
    for cid, sub in latest.items():
        if sub.status == "suspended":
            result[cid] = {"state": "suspended", "since": _taipei_date_str(sub.suspended_at),
                           "serial": sub.camera_serial}
        elif sub.status == "ended" and sub.lock_released_at is None:
            result[cid] = {"state": "ended", "since": _taipei_date_str(sub.ended_at),
                           "serial": sub.camera_serial}
    _lock_cache["map"] = result
    _lock_cache["at"] = now
    return result


def is_exempt(user: User) -> bool:
    return user.role == "symotus_admin"


def assert_camera_unlocked(db: Session, user: User, camera_id: int) -> None:
    """非管理員碰鎖定相機 → 403 CAMERA_LOCKED（前端依 code 顯示鎖定畫面）。"""
    if is_exempt(user):
        return
    info = get_lock_map(db).get(int(camera_id))
    if info:
        raise HTTPException(403, detail={
            "code": "CAMERA_LOCKED", "state": info["state"], "since": info["since"],
            "message": "此相機服務已暫停" if info["state"] == "suspended" else "此相機訂閱已結束",
        })


def locked_camera_ids(db: Session) -> set[int]:
    return set(get_lock_map(db).keys())


def locked_serial_in_path(db: Session, path: str) -> bool:
    """NAS 單張照片只帶路徑（/homes/firmness/{serial}/...），以 serial 反查是否屬於鎖定相機。"""
    if not path:
        return False
    for info in get_lock_map(db).values():
        serial = info.get("serial")
        if serial and f"/{serial}/" in f"{path}/":
            return True
    return False


def camera_billing_flags(db: Session, user: User, today: Optional[date] = None) -> dict[int, dict]:
    """{camera_id: {"overdue": bool, "payment_pending": bool}}，只對付款人（reseller）與 admin。

    end_user 一律空：不向 reseller 的客戶揭露 reseller 的欠款（spec §10.1）。"""
    if user.role not in ("reseller", "symotus_admin"):
        return {}
    today = today or taipei_today()
    q = db.query(BillingBill).filter(BillingBill.status == "unpaid", BillingBill.due_date < today)
    if user.role == "reseller":
        q = q.filter(BillingBill.customer_id == user.id)
    try:
        rows = q.all()
    except Exception as e:  # noqa: BLE001
        _safe_rollback(db)
        logger.error("billing: 讀取逾期狀態失敗：%s", e)
        return {}
    flags: dict[int, dict] = {}
    for b in rows:
        f = flags.setdefault(b.camera_id, {"overdue": True, "payment_pending": True})
        if b.pending_report_id is None:
            f["payment_pending"] = False
    return flags


def billing_field(db: Session, user: User, camera_id: int,
                  lock_map: Optional[dict] = None, flags: Optional[dict] = None) -> dict:
    """相機清單每台附的 billing 欄位（spec §11.3）。"""
    lock_map = get_lock_map(db) if lock_map is None else lock_map
    flags = camera_billing_flags(db, user) if flags is None else flags
    info = lock_map.get(camera_id)
    f = flags.get(camera_id, {})
    return {
        "locked": info["state"] if info else None,
        "since": info["since"] if info else None,
        "overdue": bool(f.get("overdue")),
        "payment_pending": bool(f.get("payment_pending")),
    }
