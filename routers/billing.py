"""計費 v2 API：按相機訂閱、週年帳單日、逾期處置、付款回報。

規格：symotus-frontend/docs/superpowers/specs/2026-10-07-camera-subscription-billing-design.md §11.3

權限原則：
- /billing/admin/* 一律 require_role("symotus_admin")，與前端顯不顯示無關。
- /billing/my/* 與 /billing/notices/my 只回 current_user 自己的資料；用他人的 bill_id／report_id
  一律回 404（不透露存在與否）。
- 金錢狀態只由 admin 改變：reseller 的付款回報不會讓帳單變已繳（spec §6.5）。
"""
import hashlib
import logging
import re
from datetime import date, datetime, timedelta
from typing import Literal, Optional

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from audit import log_action
from auth import get_current_user, require_role
from database import get_db
from models import (
    BillingBill, BillingPaymentReceipt, BillingPaymentReport, BillingPaymentReportBill,
    BillingPlanV2, BillingSetting, BillingSubscriptionV2, CameraAccess, User,
)
from services.billing_dates import (
    next_bill_date_after, overdue_days, taipei_date_to_utc_naive, taipei_today, term_end_of, valid_combo,
)
from services.billing_v2 import (
    OPEN_STATUSES, advance_one, get_lock_map, invalidate_lock_cache,
)

router = APIRouter(prefix="/billing", tags=["billing"])
logger = logging.getLogger(__name__)

ADMIN = require_role("symotus_admin")

MAX_BACKDATE_DAYS = 92          # 建立訂閱時起始日最多回溯（spec §7）
MAX_PENDING_REPORTS = 10        # 每位 reseller 同時最多幾份待確認回報（spec §6.5）
MAX_RECEIPT_BYTES = 2 * 1024 * 1024
PENDING_ALERT_DAYS = 3          # 待確認超過幾天標紅
LAST5_RE = re.compile(r"^[0-9]{5}$")
SETTING_KEYS = ("payment_instructions",)


# ── 共用小工具 ────────────────────────────────────────────────────────

def _user_names(db: Session, ids) -> dict[int, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {u.id: u.username for u in db.query(User).filter(User.id.in_(ids)).all()}


def _iso(d) -> Optional[str]:
    return d.isoformat() if d else None


def _utc_iso(ts: Optional[datetime]) -> Optional[str]:
    return ts.isoformat() + "Z" if ts else None


def _bill_state(b: BillingBill, today: date) -> str:
    """顯示狀態：paid / void / pending（付款確認中）/ overdue / due（待繳、未到期）。"""
    if b.status == "paid":
        return "paid"
    if b.status == "void":
        return "void"
    if b.pending_report_id:
        return "pending"
    return "overdue" if today > b.due_date else "due"


def _bill_out(b: BillingBill, today: date, sub_status: Optional[str] = None) -> dict:
    return {
        "id": b.id,
        "subscription_id": b.subscription_id,
        "camera_id": b.camera_id,
        "customer_id": b.customer_id,
        "camera_name": b.camera_name,
        "plan_name": b.plan_name,
        "cycle": b.cycle,
        "period_start": _iso(b.period_start),
        "period_end": _iso(b.period_end),
        "due_date": _iso(b.due_date),
        "amount": b.amount,
        "status": b.status,
        "state": _bill_state(b, today),
        "overdue": b.status == "unpaid" and today > b.due_date,
        "overdue_days": overdue_days(b.due_date, today) if b.status == "unpaid" else 0,
        "paid_on": _iso(b.paid_on),
        "paid_at": _utc_iso(b.paid_at),
        "paid_note": b.paid_note,
        "paid_late": bool(b.status == "paid" and b.paid_on and b.paid_on > b.due_date),
        "paid_via_report_id": b.paid_via_report_id,
        "pending_report_id": b.pending_report_id,
        "void_reason": b.void_reason,
        "subscription_status": sub_status,
    }


def _plan_out(p: BillingPlanV2) -> dict:
    return {"id": p.id, "name": p.name, "description": p.description, "term": p.term,
            "cycle": p.cycle, "price": p.price, "is_active": p.is_active}


def _sub_out(db: Session, s: BillingSubscriptionV2, today: date, names: Optional[dict] = None,
             lock_map: Optional[dict] = None) -> dict:
    names = names if names is not None else _user_names(db, [s.customer_id])
    lock_map = lock_map if lock_map is not None else get_lock_map(db)
    unpaid = db.query(BillingBill).filter(
        BillingBill.subscription_id == s.id, BillingBill.status == "unpaid").all()
    overdue = [b for b in unpaid if today > b.due_date]
    next_bill = None
    if s.status in ("scheduled", "active", "suspended"):
        next_bill = s.start_date if s.start_date > today else next_bill_date_after(
            s.start_date, s.cycle, s.anchor_day, today)
        if s.cancel_at and next_bill >= s.cancel_at:
            next_bill = None
    lock = lock_map.get(s.camera_id)
    is_latest_lock = bool(lock) and (
        (lock["state"] == "suspended" and s.status == "suspended")
        or (lock["state"] == "ended" and s.status == "ended" and s.lock_released_at is None))
    return {
        "id": s.id,
        "camera_id": s.camera_id,
        "camera_name": s.camera_name or f"相機 #{s.camera_id}",
        "customer_id": s.customer_id,
        "customer_name": names.get(s.customer_id),
        "plan_id": s.plan_id,
        "plan_name": s.plan_name,
        "term": s.term,
        "cycle": s.cycle,
        "price": s.price,
        "start_date": _iso(s.start_date),
        "anchor_day": s.anchor_day,
        "term_start": _iso(s.term_start),
        "term_end": _iso(term_end_of(s.term_start, s.anchor_day)) if s.term == "annual" else None,
        "auto_renew": s.auto_renew,
        "cancel_at": _iso(s.cancel_at),
        "status": s.status,
        "suspended_at": _utc_iso(s.suspended_at),
        "ended_at": _utc_iso(s.ended_at),
        "end_reason": s.end_reason,
        "lock_released_at": _utc_iso(s.lock_released_at),
        "locked": lock["state"] if is_latest_lock else None,
        "next_bill_date": _iso(next_bill),
        "unpaid_count": len(unpaid),
        "unpaid_total": sum(b.amount for b in unpaid),
        "overdue_count": len(overdue),
        "overdue_total": sum(b.amount for b in overdue),
        "max_overdue_days": max((overdue_days(b.due_date, today) for b in overdue), default=0),
        "note": s.note,
        "created_at": _utc_iso(s.created_at),
    }


def _get_setting(db: Session, key: str) -> Optional[str]:
    row = db.query(BillingSetting).filter(BillingSetting.key == key).first()
    return row.value if row else None


def _maybe_resume(db: Session, sub_ids, today: date, actor: User) -> list[int]:
    """暫停中的訂閱若已無逾期帳單 → 恢復。只在 admin 勾了「同時恢復服務」時呼叫（spec §9.4）。"""
    db.flush()  # SessionLocal 是 autoflush=False：剛標成 paid 的帳單要先 flush，下面的 count 才看得到
    resumed = []
    for sid in set(sub_ids):
        sub = db.query(BillingSubscriptionV2).filter(BillingSubscriptionV2.id == sid).first()
        if not sub or sub.status != "suspended":
            continue
        still = db.query(BillingBill).filter(
            BillingBill.subscription_id == sid, BillingBill.status == "unpaid",
            BillingBill.due_date < today).count()
        if still:
            continue
        sub.status = "active"
        sub.suspended_at = None
        log_action(db, actor, "billing.subscription.resume", "billing_subscription", sid, "after payment")
        resumed.append(sid)
    return resumed


async def _resolve_camera(camera_id: int) -> tuple[Optional[str], Optional[str]]:
    """用 admin token 取相機名稱與 NAS serial（快照與鎖定 /nas/image 用）。失敗回 (None, None)。"""
    from routers.cameras import CAMERA_BACKEND_URL, _get_admin_camera_token
    tok = await _get_admin_camera_token()
    if not tok:
        return None, None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{CAMERA_BACKEND_URL}/api/cameras/{camera_id}",
                                 headers={"Authorization": f"Bearer {tok}"})
        if r.status_code != 200:
            return None, None
        basic = r.json().get("basic_info", r.json())
        serial = basic.get("device_serial_id") or basic.get("serial_id") or basic.get("serial")
        return basic.get("name"), serial
    except Exception as e:  # noqa: BLE001
        logger.warning("billing: 取相機 %s 資訊失敗：%s", camera_id, e)
        return None, None


async def _fetch_all_cameras() -> list[dict]:
    from routers.cameras import CAMERA_BACKEND_URL, _get_admin_camera_token
    tok = await _get_admin_camera_token()
    if not tok:
        return []
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{CAMERA_BACKEND_URL}/api/cameras",
                             headers={"Authorization": f"Bearer {tok}"}, params={"limit": 1000})
    if r.status_code != 200:
        return []
    return r.json().get("cameras", [])


async def _ensure_serial(db: Session, sub: BillingSubscriptionV2) -> None:
    if sub.camera_serial:
        return
    name, serial = await _resolve_camera(sub.camera_id)
    if serial:
        sub.camera_serial = serial
    if name and not sub.camera_name:
        sub.camera_name = name


# ── 方案 ──────────────────────────────────────────────────────────────

class PlanIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=1000)
    term: Literal["monthly", "annual"]
    cycle: Literal["monthly", "yearly"]
    price: int = Field(ge=0, le=10_000_000)
    is_active: bool = True


class PlanPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=1000)
    price: Optional[int] = Field(default=None, ge=0, le=10_000_000)
    is_active: Optional[bool] = None


@router.get("/admin/plans")
def list_plans(include_inactive: bool = False, db: Session = Depends(get_db), _: User = Depends(ADMIN)):
    q = db.query(BillingPlanV2)
    if not include_inactive:
        q = q.filter(BillingPlanV2.is_active == True)  # noqa: E712
    return [_plan_out(p) for p in q.order_by(BillingPlanV2.id).all()]


@router.post("/admin/plans")
def create_plan(body: PlanIn, db: Session = Depends(get_db), me: User = Depends(ADMIN)):
    if not valid_combo(body.term, body.cycle):
        raise HTTPException(422, "月約不可年繳（請改用年約年繳）")
    p = BillingPlanV2(**body.model_dump())
    db.add(p)
    db.flush()
    log_action(db, me, "billing.plan.create", "billing_plan", p.id, f"{p.name} {p.term}/{p.cycle} {p.price}")
    db.commit()
    return _plan_out(p)


@router.put("/admin/plans/{plan_id}")
def update_plan(plan_id: int, body: PlanPatch, db: Session = Depends(get_db), me: User = Depends(ADMIN)):
    """合約期與週期建立後不可改（已綁訂閱的語意會被改掉）；要換組合就新建方案。改價不影響既有訂閱。"""
    p = db.query(BillingPlanV2).filter(BillingPlanV2.id == plan_id).first()
    if not p:
        raise HTTPException(404, "方案不存在")
    changes = body.model_dump(exclude_unset=True)
    for k, v in changes.items():
        setattr(p, k, v)
    log_action(db, me, "billing.plan.update", "billing_plan", p.id, str(changes))
    db.commit()
    return _plan_out(p)


@router.delete("/admin/plans/{plan_id}")
def deactivate_plan(plan_id: int, db: Session = Depends(get_db), me: User = Depends(ADMIN)):
    p = db.query(BillingPlanV2).filter(BillingPlanV2.id == plan_id).first()
    if not p:
        raise HTTPException(404, "方案不存在")
    p.is_active = False
    log_action(db, me, "billing.plan.deactivate", "billing_plan", p.id, p.name)
    db.commit()
    return {"ok": True}


# ── 訂閱 ──────────────────────────────────────────────────────────────

class SubscriptionIn(BaseModel):
    camera_ids: list[int] = Field(min_length=1, max_length=50)
    customer_id: int
    plan_id: int
    start_date: date
    price: Optional[int] = Field(default=None, ge=0, le=10_000_000)
    auto_renew: bool = True
    note: Optional[str] = Field(default=None, max_length=1000)
    mark_past_due_paid: bool = False   # 補登歷史訂閱時，把已過截止日的帳單標成已收款（spec §13 S7）


class EndIn(BaseModel):
    reason: Literal["terminated", "non_payment"] = "terminated"
    bill_actions: dict[int, Literal["keep", "void"]] = {}


class ScheduleCancelIn(BaseModel):
    cancel: bool


@router.get("/admin/resellers")
def list_resellers(db: Session = Depends(get_db), _: User = Depends(ADMIN)):
    """建立訂閱時可選的付款人：只能是 reseller（spec §13 S10）。"""
    rows = db.query(User).filter(User.role == "reseller", User.is_active == True).order_by(User.username).all()  # noqa: E712
    return [{"id": u.id, "username": u.username, "email": u.email} for u in rows]


@router.get("/admin/unsubscribed-cameras")
async def unsubscribed_cameras(db: Session = Depends(get_db), _: User = Depends(ADMIN)):
    """尚未納管的相機（沒有 scheduled/active/suspended 訂閱）＋ 可能的付款人（持自我配對 grant 的 reseller）。"""
    cams = await _fetch_all_cameras()
    open_ids = {r[0] for r in db.query(BillingSubscriptionV2.camera_id)
                .filter(BillingSubscriptionV2.status.in_(OPEN_STATUSES)).all()}
    lock_map = get_lock_map(db)
    resellers = {u.id: u.username for u in db.query(User).filter(User.role == "reseller").all()}
    owners: dict[int, list[dict]] = {}
    for a in db.query(CameraAccess).filter(CameraAccess.granted_by == CameraAccess.user_id).all():
        if a.user_id in resellers:
            owners.setdefault(a.camera_id, []).append({"id": a.user_id, "username": resellers[a.user_id]})
    out = []
    for c in cams:
        cid = c.get("id")
        if cid is None or cid in open_ids:
            continue
        out.append({"camera_id": cid, "camera_name": c.get("name") or f"相機 #{cid}",
                    "locked": (lock_map.get(cid) or {}).get("state"),
                    "owners": owners.get(cid, [])})
    return out


@router.get("/admin/subscriptions")
def list_subscriptions(
    status: Optional[str] = None,
    customer_id: Optional[int] = None,
    camera_id: Optional[int] = None,
    db: Session = Depends(get_db), _: User = Depends(ADMIN),
):
    q = db.query(BillingSubscriptionV2)
    if status:
        q = q.filter(BillingSubscriptionV2.status == status)
    if customer_id:
        q = q.filter(BillingSubscriptionV2.customer_id == customer_id)
    if camera_id:
        q = q.filter(BillingSubscriptionV2.camera_id == camera_id)
    subs = q.order_by(BillingSubscriptionV2.id.desc()).all()
    today = taipei_today()
    names = _user_names(db, [s.customer_id for s in subs])
    lock_map = get_lock_map(db)
    return [_sub_out(db, s, today, names, lock_map) for s in subs]


@router.get("/admin/subscriptions/{sub_id}")
def get_subscription(sub_id: int, db: Session = Depends(get_db), _: User = Depends(ADMIN)):
    s = db.query(BillingSubscriptionV2).filter(BillingSubscriptionV2.id == sub_id).first()
    if not s:
        raise HTTPException(404, "訂閱不存在")
    today = taipei_today()
    out = _sub_out(db, s, today)
    bills = db.query(BillingBill).filter(BillingBill.subscription_id == s.id).order_by(
        BillingBill.period_start.desc()).all()
    out["bills"] = [_bill_out(b, today, s.status) for b in bills]
    return out


@router.post("/admin/subscriptions")
async def create_subscriptions(body: SubscriptionIn, db: Session = Depends(get_db), me: User = Depends(ADMIN)):
    today = taipei_today()
    customer = db.query(User).filter(User.id == body.customer_id).first()
    if not customer or customer.role != "reseller" or not customer.is_active:
        raise HTTPException(422, "付款人必須是啟用中的 reseller")
    plan = db.query(BillingPlanV2).filter(BillingPlanV2.id == body.plan_id).first()
    if not plan or not plan.is_active:
        raise HTTPException(422, "方案不存在或已停用")
    if body.start_date < today - timedelta(days=MAX_BACKDATE_DAYS):
        raise HTTPException(422, f"起始日最多回溯 {MAX_BACKDATE_DAYS} 天")
    if len(set(body.camera_ids)) != len(body.camera_ids):
        raise HTTPException(422, "相機重複")

    results = []
    for cid in body.camera_ids:
        if db.query(BillingSubscriptionV2).filter(
                BillingSubscriptionV2.camera_id == cid,
                BillingSubscriptionV2.status.in_(OPEN_STATUSES)).first():
            results.append({"camera_id": cid, "ok": False, "error": "這台相機已有生效中的訂閱"})
            continue
        name, serial = await _resolve_camera(cid)
        sub = BillingSubscriptionV2(
            camera_id=cid, camera_name=name or f"相機 #{cid}", camera_serial=serial,
            customer_id=customer.id, plan_id=plan.id, plan_name=plan.name,
            term=plan.term, cycle=plan.cycle,
            price=plan.price if body.price is None else body.price,
            start_date=body.start_date, anchor_day=body.start_date.day, term_start=body.start_date,
            auto_renew=body.auto_renew,
            status="scheduled" if body.start_date > today else "active",
            note=body.note, created_by=me.id,
        )
        db.add(sub)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            results.append({"camera_id": cid, "ok": False, "error": "這台相機已有生效中的訂閱"})
            continue
        log_action(db, me, "billing.subscription.create", "billing_subscription", sub.id,
                   f"camera={cid} customer={customer.id} plan={plan.id} price={sub.price} start={sub.start_date}")
        db.commit()
        created = advance_one(db, sub, today)
        marked = 0
        if body.mark_past_due_paid:
            for b in db.query(BillingBill).filter(
                    BillingBill.subscription_id == sub.id, BillingBill.status == "unpaid",
                    BillingBill.due_date < today).all():
                b.status, b.paid_on, b.paid_at = "paid", b.due_date, datetime.utcnow()
                b.paid_note = "補登訂閱時標記為已收款"
                marked += 1
            if marked:
                log_action(db, me, "billing.bill.mark_paid", "billing_subscription", sub.id,
                           f"backfill {marked} bills")
            db.commit()
        results.append({"camera_id": cid, "ok": True, "subscription_id": sub.id,
                        "bills_created": created, "bills_marked_paid": marked})
    invalidate_lock_cache()
    return {"results": results}


def _load_sub_for_update(db: Session, sub_id: int) -> BillingSubscriptionV2:
    s = db.query(BillingSubscriptionV2).filter(BillingSubscriptionV2.id == sub_id).with_for_update().first()
    if not s:
        raise HTTPException(404, "訂閱不存在")
    return s


async def _suspend(db: Session, sub_id: int, me: User) -> BillingSubscriptionV2:
    s = _load_sub_for_update(db, sub_id)
    if s.status != "active":
        raise HTTPException(409, f"目前狀態為 {s.status}，無法暫停")
    s.status = "suspended"
    s.suspended_at = datetime.utcnow()
    await _ensure_serial(db, s)
    log_action(db, me, "billing.subscription.suspend", "billing_subscription", s.id, f"camera={s.camera_id}")
    db.commit()
    invalidate_lock_cache()
    return s


async def _end_sub(db: Session, sub_id: int, body: EndIn, me: User) -> BillingSubscriptionV2:
    s = _load_sub_for_update(db, sub_id)
    if s.status not in OPEN_STATUSES:
        raise HTTPException(409, f"目前狀態為 {s.status}，無法結束")
    unpaid = {b.id: b for b in db.query(BillingBill).filter(
        BillingBill.subscription_id == s.id, BillingBill.status == "unpaid").all()}
    for bid, act in body.bill_actions.items():
        if bid not in unpaid:
            raise HTTPException(422, f"帳單 #{bid} 不是這份訂閱的未繳帳單")
        if act == "void" and unpaid[bid].pending_report_id:
            raise HTTPException(409, f"帳單 #{bid} 有待確認的付款回報，請先審核再作廢")
    for bid, act in body.bill_actions.items():
        if act == "void":
            b = unpaid[bid]
            b.status, b.voided_at, b.void_reason = "void", datetime.utcnow(), "結束訂閱時作廢"
    today = taipei_today()
    s.status = "ended"
    s.ended_at = taipei_date_to_utc_naive(today)
    s.end_reason = body.reason
    s.cancel_at = None
    await _ensure_serial(db, s)
    voided = [b for b, a in body.bill_actions.items() if a == "void"]
    log_action(db, me, "billing.subscription.end", "billing_subscription", s.id,
               f"camera={s.camera_id} reason={body.reason} voided={voided}")
    db.commit()
    invalidate_lock_cache()
    return s


@router.post("/admin/subscriptions/{sub_id}/suspend")
async def suspend_subscription(sub_id: int, db: Session = Depends(get_db), me: User = Depends(ADMIN)):
    s = await _suspend(db, sub_id, me)
    return _sub_out(db, s, taipei_today())


@router.post("/admin/subscriptions/{sub_id}/resume")
def resume_subscription(sub_id: int, db: Session = Depends(get_db), me: User = Depends(ADMIN)):
    s = _load_sub_for_update(db, sub_id)
    if s.status != "suspended":
        raise HTTPException(409, f"目前狀態為 {s.status}，無法恢復")
    s.status = "active"
    s.suspended_at = None
    log_action(db, me, "billing.subscription.resume", "billing_subscription", s.id, f"camera={s.camera_id}")
    db.commit()
    advance_one(db, s)
    return _sub_out(db, s, taipei_today())


@router.post("/admin/subscriptions/{sub_id}/end")
async def end_subscription(sub_id: int, body: EndIn, db: Session = Depends(get_db), me: User = Depends(ADMIN)):
    s = await _end_sub(db, sub_id, body, me)
    return _sub_out(db, s, taipei_today())


@router.post("/admin/subscriptions/{sub_id}/schedule-cancel")
def schedule_cancel(sub_id: int, body: ScheduleCancelIn, db: Session = Depends(get_db), me: User = Depends(ADMIN)):
    """到期不續約：月約在下一個帳單日結束；年約在合約到期日結束。到期當天起相機鎖定（spec D9）。"""
    s = _load_sub_for_update(db, sub_id)
    if s.status not in ("active", "suspended"):
        raise HTTPException(409, f"目前狀態為 {s.status}，無法設定不續約")
    today = taipei_today()
    if body.cancel:
        s.cancel_at = (term_end_of(s.term_start, s.anchor_day) if s.term == "annual"
                       else next_bill_date_after(s.start_date, s.cycle, s.anchor_day, today))
    else:
        s.cancel_at = None
    log_action(db, me, "billing.subscription.schedule_cancel", "billing_subscription", s.id,
               f"cancel_at={s.cancel_at}")
    db.commit()
    return _sub_out(db, s, today)


@router.post("/admin/subscriptions/{sub_id}/release-lock")
def release_lock(sub_id: int, db: Session = Depends(get_db), me: User = Depends(ADMIN)):
    """已結束的訂閱解除鎖定：相機回到未納管，不計費、所有人照常使用（spec §6.3）。"""
    s = _load_sub_for_update(db, sub_id)
    if s.status != "ended":
        raise HTTPException(409, "只有已結束的訂閱可以解除鎖定")
    if s.lock_released_at is None:
        s.lock_released_at = datetime.utcnow()
        log_action(db, me, "billing.subscription.release_lock", "billing_subscription", s.id,
                   f"camera={s.camera_id}")
        db.commit()
    invalidate_lock_cache()
    return _sub_out(db, s, taipei_today())


# ── 帳單 ──────────────────────────────────────────────────────────────

class MarkPaidIn(BaseModel):
    bill_ids: list[int] = Field(min_length=1, max_length=200)
    paid_on: date
    paid_note: Optional[str] = Field(default=None, max_length=500)
    resume_subscriptions: bool = False


class VoidIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class AmountIn(BaseModel):
    amount: int = Field(ge=0, le=10_000_000)
    reason: str = Field(min_length=1, max_length=500)


@router.get("/admin/bills")
def list_bills(
    status: Optional[Literal["unpaid", "paid", "void"]] = None,
    overdue: Optional[bool] = None,
    customer_id: Optional[int] = None,
    subscription_id: Optional[int] = None,
    due_from: Optional[date] = None,
    due_to: Optional[date] = None,
    db: Session = Depends(get_db), _: User = Depends(ADMIN),
):
    today = taipei_today()
    q = db.query(BillingBill)
    if status:
        q = q.filter(BillingBill.status == status)
    if overdue is True:
        q = q.filter(BillingBill.status == "unpaid", BillingBill.due_date < today)
    if customer_id:
        q = q.filter(BillingBill.customer_id == customer_id)
    if subscription_id:
        q = q.filter(BillingBill.subscription_id == subscription_id)
    if due_from:
        q = q.filter(BillingBill.due_date >= due_from)
    if due_to:
        q = q.filter(BillingBill.due_date <= due_to)
    bills = q.order_by(BillingBill.due_date.desc(), BillingBill.id.desc()).limit(500).all()
    names = _user_names(db, [b.customer_id for b in bills])
    statuses = {s.id: s.status for s in db.query(BillingSubscriptionV2).filter(
        BillingSubscriptionV2.id.in_({b.subscription_id for b in bills} or {0})).all()}
    out = []
    for b in bills:
        row = _bill_out(b, today, statuses.get(b.subscription_id))
        row["customer_name"] = names.get(b.customer_id)
        out.append(row)
    return out


def _lock_bills(db: Session, ids) -> dict[int, BillingBill]:
    rows = db.query(BillingBill).filter(BillingBill.id.in_(set(ids))).with_for_update().all()
    return {b.id: b for b in rows}


@router.post("/admin/bills/mark-paid")
def mark_paid(body: MarkPaidIn, db: Session = Depends(get_db), me: User = Depends(ADMIN)):
    """直接標記收款（沒有付款回報時用）。在 pending 回報中的帳單一律 409，避免同一筆錢記兩次。"""
    today = taipei_today()
    if body.paid_on > today:
        raise HTTPException(422, "付款日不可晚於今天")
    bills = _lock_bills(db, body.bill_ids)
    missing = set(body.bill_ids) - set(bills)
    if missing:
        raise HTTPException(404, f"帳單不存在：{sorted(missing)}")
    bad = [b.id for b in bills.values() if b.status != "unpaid"]
    if bad:
        raise HTTPException(409, f"帳單 {bad} 不是未繳狀態")
    pending = [b.id for b in bills.values() if b.pending_report_id]
    if pending:
        raise HTTPException(409, {"code": "BILL_IN_PENDING_REPORT", "bill_ids": pending,
                                  "message": "這些帳單有待確認的付款回報，請從付款確認處理"})
    now = datetime.utcnow()
    for b in bills.values():
        b.status, b.paid_on, b.paid_at, b.paid_note = "paid", body.paid_on, now, body.paid_note
    log_action(db, me, "billing.bill.mark_paid", "billing_bill", None,
               f"bills={sorted(bills)} paid_on={body.paid_on}")
    resumed = _maybe_resume(db, [b.subscription_id for b in bills.values()], today, me) \
        if body.resume_subscriptions else []
    db.commit()
    invalidate_lock_cache()
    return {"paid": len(bills), "resumed": resumed}


@router.post("/admin/bills/{bill_id}/void")
def void_bill(bill_id: int, body: VoidIn, db: Session = Depends(get_db), me: User = Depends(ADMIN)):
    b = _lock_bills(db, [bill_id]).get(bill_id)
    if not b:
        raise HTTPException(404, "帳單不存在")
    if b.status == "paid":
        raise HTTPException(409, "已收款的帳單不可作廢")
    if b.status == "void":
        raise HTTPException(409, "帳單已作廢")
    if b.pending_report_id:
        raise HTTPException(409, "這張帳單有待確認的付款回報，請先確認或退回")
    b.status, b.voided_at, b.void_reason = "void", datetime.utcnow(), body.reason
    log_action(db, me, "billing.bill.void", "billing_bill", b.id, body.reason)
    db.commit()
    return _bill_out(b, taipei_today())


@router.put("/admin/bills/{bill_id}/amount")
def change_amount(bill_id: int, body: AmountIn, db: Session = Depends(get_db), me: User = Depends(ADMIN)):
    b = _lock_bills(db, [bill_id]).get(bill_id)
    if not b:
        raise HTTPException(404, "帳單不存在")
    if b.status != "unpaid":
        raise HTTPException(409, "只有未繳帳單可以改金額")
    if b.pending_report_id:
        raise HTTPException(409, "這張帳單有待確認的付款回報，請先確認或退回")
    old = b.amount
    b.amount = body.amount
    log_action(db, me, "billing.bill.amount", "billing_bill", b.id, f"{old} -> {body.amount}：{body.reason}")
    db.commit()
    return _bill_out(b, taipei_today())


# ── 逾期處置 ──────────────────────────────────────────────────────────

class OverdueAction(BaseModel):
    subscription_id: int
    action: Literal["none", "suspend", "end"]
    bill_actions: dict[int, Literal["keep", "void"]] = {}


class OverdueApplyIn(BaseModel):
    actions: list[OverdueAction] = Field(min_length=1, max_length=200)


def _overdue_groups(db: Session, today: date) -> dict:
    bills = db.query(BillingBill).filter(
        BillingBill.status == "unpaid", BillingBill.due_date < today).all()
    sub_ids = {b.subscription_id for b in bills}
    subs = {s.id: s for s in db.query(BillingSubscriptionV2).filter(
        BillingSubscriptionV2.id.in_(sub_ids or {0})).all()}
    report_ids = {b.pending_report_id for b in bills if b.pending_report_id}
    reports = {r.id: r for r in db.query(BillingPaymentReport).filter(
        BillingPaymentReport.id.in_(report_ids or {0})).all()}
    names = _user_names(db, [b.customer_id for b in bills])
    customers: dict[int, dict] = {}
    for b in sorted(bills, key=lambda x: x.due_date):
        s = subs.get(b.subscription_id)
        c = customers.setdefault(b.customer_id, {
            "customer_id": b.customer_id, "customer_name": names.get(b.customer_id),
            "total": 0, "subscriptions": {}})
        row = c["subscriptions"].setdefault(b.subscription_id, {
            "subscription_id": b.subscription_id, "camera_id": b.camera_id,
            "camera_name": (s.camera_name if s else None) or b.camera_name,
            "plan_name": s.plan_name if s else b.plan_name,
            "term": s.term if s else None, "cycle": b.cycle,
            "status": s.status if s else None,
            "suspended_at": _utc_iso(s.suspended_at) if s else None,
            "ended_at": _utc_iso(s.ended_at) if s else None,
            "bill_count": 0, "total": 0, "max_overdue_days": 0,
            "bills": [], "all_pending": True, "reported_at": None})
        row["bill_count"] += 1
        row["total"] += b.amount
        row["max_overdue_days"] = max(row["max_overdue_days"], overdue_days(b.due_date, today))
        rep = reports.get(b.pending_report_id) if b.pending_report_id else None
        row["bills"].append({"id": b.id, "amount": b.amount, "due_date": _iso(b.due_date),
                             "period_start": _iso(b.period_start), "period_end": _iso(b.period_end),
                             "pending_report_id": b.pending_report_id})
        if not rep:
            row["all_pending"] = False
        elif row["reported_at"] is None or _utc_iso(rep.created_at) < row["reported_at"]:
            row["reported_at"] = _utc_iso(rep.created_at)
        c["total"] += b.amount
    out = []
    undecided = 0
    for c in customers.values():
        rows = sorted(c["subscriptions"].values(), key=lambda r: -r["max_overdue_days"])
        undecided += sum(1 for r in rows if r["status"] == "active")
        out.append({**c, "subscriptions": rows})
    out.sort(key=lambda c: -c["total"])
    return {"customers": out, "undecided": undecided, "total": sum(c["total"] for c in out),
            "camera_count": sum(len(c["subscriptions"]) for c in out)}


@router.get("/admin/overdue")
def overdue_list(db: Session = Depends(get_db), _: User = Depends(ADMIN)):
    return _overdue_groups(db, taipei_today())


@router.post("/admin/overdue/apply")
async def overdue_apply(body: OverdueApplyIn, db: Session = Depends(get_db), me: User = Depends(ADMIN)):
    """逐筆執行、逐筆回結果：部分失敗不可整體顯示成功（spec §9.3）。"""
    results = []
    for a in body.actions:
        if a.action == "none":
            results.append({"subscription_id": a.subscription_id, "ok": True, "action": "none"})
            continue
        try:
            if a.action == "suspend":
                await _suspend(db, a.subscription_id, me)
            else:
                await _end_sub(db, a.subscription_id, EndIn(reason="non_payment", bill_actions=a.bill_actions), me)
            results.append({"subscription_id": a.subscription_id, "ok": True, "action": a.action})
        except HTTPException as e:
            db.rollback()
            detail = e.detail if isinstance(e.detail, str) else str(e.detail)
            results.append({"subscription_id": a.subscription_id, "ok": False, "action": a.action,
                            "error": detail})
    return {"results": results}


# ── 付款回報（reseller）───────────────────────────────────────────────

def _sniff_receipt(data: bytes) -> Optional[str]:
    """以檔頭判斷類型，不信任 Content-Type 與副檔名。"""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    return None


def _report_out(db: Session, r: BillingPaymentReport, today: date, names: Optional[dict] = None) -> dict:
    links = db.query(BillingPaymentReportBill).filter(BillingPaymentReportBill.report_id == r.id).all()
    bills = {b.id: b for b in db.query(BillingBill).filter(
        BillingBill.id.in_({l.bill_id for l in links} or {0})).all()}
    has_receipt = db.query(BillingPaymentReceipt.report_id).filter(
        BillingPaymentReceipt.report_id == r.id).first() is not None
    sub_status = {s.id: s.status for s in db.query(BillingSubscriptionV2).filter(
        BillingSubscriptionV2.id.in_({b.subscription_id for b in bills.values()} or {0})).all()}
    bill_rows = []
    for l in links:
        b = bills.get(l.bill_id)
        if not b:
            continue
        row = _bill_out(b, today, sub_status.get(b.subscription_id))
        row["amount_snapshot"] = l.amount_snapshot
        row["confirmed"] = l.confirmed
        bill_rows.append(row)
    bill_rows.sort(key=lambda x: x["period_start"] or "")
    bills_total = sum(l.amount_snapshot for l in links)
    names = names if names is not None else _user_names(db, [r.customer_id, r.reviewed_by])
    return {
        "id": r.id, "customer_id": r.customer_id, "customer_name": names.get(r.customer_id),
        "paid_on": _iso(r.paid_on), "amount": r.amount, "method": r.method,
        "account_last5": r.account_last5, "note": r.note, "status": r.status,
        "reviewed_by": r.reviewed_by, "reviewed_by_name": names.get(r.reviewed_by),
        "reviewed_at": _utc_iso(r.reviewed_at), "review_note": r.review_note,
        "created_at": _utc_iso(r.created_at),
        "waiting_days": (today - taipei_today(r.created_at)).days if r.status == "pending" else None,
        "bills": bill_rows, "bills_total": bills_total, "difference": r.amount - bills_total,
        "has_receipt": has_receipt,
    }


@router.post("/my/payment-reports")
async def create_payment_report(
    bill_ids: str = Form(...),
    paid_on: date = Form(...),
    amount: int = Form(...),
    method: str = Form(...),
    account_last5: Optional[str] = Form(None),
    note: Optional[str] = Form(None),
    receipt: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    if me.role != "reseller":
        raise HTTPException(403, "只有付款人（reseller）可以回報付款")
    today = taipei_today()
    try:
        ids = sorted({int(x) for x in bill_ids.split(",") if x.strip()})
    except ValueError:
        raise HTTPException(422, "帳單編號格式錯誤")
    if not ids:
        raise HTTPException(422, "請至少選一張帳單")
    if paid_on > today:
        raise HTTPException(422, "付款日不可晚於今天")
    if amount <= 0 or amount > 100_000_000:
        raise HTTPException(422, "付款金額必須是正整數")
    if method not in ("transfer", "cash", "other"):
        raise HTTPException(422, "付款方式不正確")
    last5 = (account_last5 or "").strip() or None
    if method == "transfer" and not (last5 and LAST5_RE.match(last5)):
        raise HTTPException(422, "轉帳請填帳號末五碼（5 位數字）")
    if last5 and not LAST5_RE.match(last5):
        raise HTTPException(422, "帳號末五碼必須是 5 位數字")
    note = (note or "").strip() or None
    if note and len(note) > 500:
        raise HTTPException(422, "備註最多 500 字")

    receipt_data = receipt_type = None
    if receipt is not None and receipt.filename:
        receipt_data = await receipt.read(MAX_RECEIPT_BYTES + 1)
        if len(receipt_data) > MAX_RECEIPT_BYTES:
            raise HTTPException(422, "收據檔案不可超過 2 MB")
        receipt_type = _sniff_receipt(receipt_data)
        if not receipt_type:
            raise HTTPException(422, "收據只接受 jpg、png、pdf")

    pending_count = db.query(BillingPaymentReport).filter(
        BillingPaymentReport.customer_id == me.id, BillingPaymentReport.status == "pending").count()
    if pending_count >= MAX_PENDING_REPORTS:
        raise HTTPException(409, f"待確認的付款回報已達 {MAX_PENDING_REPORTS} 份，請等候確認")

    bills = _lock_bills(db, ids)
    if set(ids) - {b.id for b in bills.values() if b.customer_id == me.id}:
        raise HTTPException(404, "帳單不存在")
    paid = [b.id for b in bills.values() if b.status != "unpaid"]
    if paid:
        raise HTTPException(409, {"code": "BILL_NOT_UNPAID", "bill_ids": paid,
                                  "message": "部分帳單已收款或已作廢"})
    busy = [b.id for b in bills.values() if b.pending_report_id]
    if busy:
        raise HTTPException(409, {"code": "BILL_IN_PENDING_REPORT", "bill_ids": busy,
                                  "message": "部分帳單已在另一份待確認的回報中"})

    r = BillingPaymentReport(customer_id=me.id, paid_on=paid_on, amount=amount, method=method,
                             account_last5=last5, note=note, status="pending")
    db.add(r)
    db.flush()
    for b in bills.values():
        db.add(BillingPaymentReportBill(report_id=r.id, bill_id=b.id, amount_snapshot=b.amount))
        b.pending_report_id = r.id
    if receipt_data:
        db.add(BillingPaymentReceipt(report_id=r.id, content_type=receipt_type, data=receipt_data,
                                     sha256=hashlib.sha256(receipt_data).hexdigest()))
    log_action(db, me, "billing.report.create", "billing_report", r.id,
               f"bills={ids} amount={amount} method={method} receipt={bool(receipt_data)}")
    db.commit()
    return _report_out(db, r, today)


@router.get("/my/payment-reports")
def my_payment_reports(db: Session = Depends(get_db), me: User = Depends(get_current_user)):
    rows = db.query(BillingPaymentReport).filter(BillingPaymentReport.customer_id == me.id).order_by(
        BillingPaymentReport.id.desc()).limit(100).all()
    today = taipei_today()
    names = _user_names(db, [me.id] + [r.reviewed_by for r in rows])
    return [_report_out(db, r, today, names) for r in rows]


def _clear_pending(db: Session, report_id: int) -> None:
    for b in db.query(BillingBill).filter(BillingBill.pending_report_id == report_id).with_for_update().all():
        b.pending_report_id = None


@router.post("/my/payment-reports/{report_id}/withdraw")
def withdraw_report(report_id: int, db: Session = Depends(get_db), me: User = Depends(get_current_user)):
    r = db.query(BillingPaymentReport).filter(
        BillingPaymentReport.id == report_id, BillingPaymentReport.customer_id == me.id).with_for_update().first()
    if not r:
        raise HTTPException(404, "回報不存在")
    if r.status != "pending":
        raise HTTPException(409, "只有待確認的回報可以撤回")
    r.status = "withdrawn"
    _clear_pending(db, r.id)
    log_action(db, me, "billing.report.withdraw", "billing_report", r.id, None)
    db.commit()
    return _report_out(db, r, taipei_today())


def _receipt_response(row: Optional[BillingPaymentReceipt]) -> Response:
    if not row:
        raise HTTPException(404, "沒有收據")
    headers = {"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"}
    if row.content_type == "application/pdf":
        headers["Content-Disposition"] = f'attachment; filename="receipt-{row.report_id}.pdf"'
    return Response(content=row.data, media_type=row.content_type, headers=headers)


@router.get("/my/payment-reports/{report_id}/receipt")
def my_receipt(report_id: int, db: Session = Depends(get_db), me: User = Depends(get_current_user)):
    r = db.query(BillingPaymentReport).filter(
        BillingPaymentReport.id == report_id, BillingPaymentReport.customer_id == me.id).first()
    if not r:
        raise HTTPException(404, "回報不存在")
    return _receipt_response(db.query(BillingPaymentReceipt).filter(
        BillingPaymentReceipt.report_id == r.id).first())


# ── 付款確認（admin）──────────────────────────────────────────────────

class ConfirmIn(BaseModel):
    bill_ids: list[int] = Field(min_length=1, max_length=200)
    paid_note: Optional[str] = Field(default=None, max_length=500)
    resume_subscriptions: bool = False


class RejectIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("退回原因必填")
        return v.strip()


@router.get("/admin/payment-reports")
def list_reports(
    status: Optional[Literal["pending", "confirmed", "rejected", "withdrawn"]] = None,
    customer_id: Optional[int] = None,
    db: Session = Depends(get_db), _: User = Depends(ADMIN),
):
    q = db.query(BillingPaymentReport)
    if status:
        q = q.filter(BillingPaymentReport.status == status)
    if customer_id:
        q = q.filter(BillingPaymentReport.customer_id == customer_id)
    # 待確認：等最久的排前面；其餘：最新的在前
    q = q.order_by(BillingPaymentReport.created_at.asc() if status == "pending"
                   else BillingPaymentReport.id.desc())
    rows = q.limit(200).all()
    today = taipei_today()
    names = _user_names(db, [r.customer_id for r in rows] + [r.reviewed_by for r in rows])
    return [_report_out(db, r, today, names) for r in rows]


@router.get("/admin/payment-reports/{report_id}")
def get_report(report_id: int, db: Session = Depends(get_db), _: User = Depends(ADMIN)):
    r = db.query(BillingPaymentReport).filter(BillingPaymentReport.id == report_id).first()
    if not r:
        raise HTTPException(404, "回報不存在")
    out = _report_out(db, r, taipei_today())
    # 確認後哪些暫停中的訂閱會變成「已無逾期」→ 前端決定要不要顯示「同時恢復服務」
    out["resumable_subscription_ids"] = _resumable_after(db, r, [b["id"] for b in out["bills"]])
    return out


def _resumable_after(db: Session, r: BillingPaymentReport, bill_ids: list[int]) -> list[int]:
    today = taipei_today()
    sub_ids = {b.subscription_id for b in db.query(BillingBill).filter(BillingBill.id.in_(set(bill_ids) or {0})).all()}
    out = []
    for s in db.query(BillingSubscriptionV2).filter(
            BillingSubscriptionV2.id.in_(sub_ids or {0}), BillingSubscriptionV2.status == "suspended").all():
        remaining = db.query(BillingBill).filter(
            BillingBill.subscription_id == s.id, BillingBill.status == "unpaid",
            BillingBill.due_date < today, ~BillingBill.id.in_(set(bill_ids) or {0})).count()
        if remaining == 0:
            out.append(s.id)
    return out


@router.get("/admin/payment-reports/{report_id}/receipt")
def admin_receipt(report_id: int, db: Session = Depends(get_db), _: User = Depends(ADMIN)):
    return _receipt_response(db.query(BillingPaymentReceipt).filter(
        BillingPaymentReceipt.report_id == report_id).first())


@router.post("/admin/payment-reports/{report_id}/confirm")
def confirm_report(report_id: int, body: ConfirmIn, db: Session = Depends(get_db), me: User = Depends(ADMIN)):
    r = db.query(BillingPaymentReport).filter(BillingPaymentReport.id == report_id).with_for_update().first()
    if not r:
        raise HTTPException(404, "回報不存在")
    if r.status != "pending":
        raise HTTPException(409, "這份回報已處理過")
    links = {l.bill_id: l for l in db.query(BillingPaymentReportBill).filter(
        BillingPaymentReportBill.report_id == r.id).all()}
    chosen = set(body.bill_ids)
    if chosen - set(links):
        raise HTTPException(422, "只能確認這份回報裡的帳單")
    bills = _lock_bills(db, links.keys())
    bad = [bid for bid in chosen if bills[bid].status != "unpaid" or bills[bid].pending_report_id != r.id]
    if bad:
        raise HTTPException(409, f"帳單 {bad} 狀態已改變，請重新整理")
    today = taipei_today()
    now = datetime.utcnow()
    auto = f"付款回報 #{r.id}：" + {"transfer": f"轉帳 末五碼 {r.account_last5}", "cash": "現金",
                                     "other": "其他"}[r.method]
    for bid, l in links.items():
        b = bills[bid]
        if b.pending_report_id == r.id:
            b.pending_report_id = None
        if bid in chosen:
            b.status, b.paid_on, b.paid_at = "paid", r.paid_on, now
            b.paid_via_report_id = r.id
            b.paid_note = auto + (f"；{body.paid_note}" if body.paid_note else "")
            l.confirmed = True
        else:
            l.confirmed = False
    r.status, r.reviewed_by, r.reviewed_at = "confirmed", me.id, now
    r.review_note = body.paid_note
    log_action(db, me, "billing.report.confirm", "billing_report", r.id,
               f"confirmed={sorted(chosen)} skipped={sorted(set(links) - chosen)}")
    resumed = _maybe_resume(db, [bills[b].subscription_id for b in chosen], today, me) \
        if body.resume_subscriptions else []
    db.commit()
    invalidate_lock_cache()
    out = _report_out(db, r, today)
    out["resumed"] = resumed
    return out


@router.post("/admin/payment-reports/{report_id}/reject")
def reject_report(report_id: int, body: RejectIn, db: Session = Depends(get_db), me: User = Depends(ADMIN)):
    r = db.query(BillingPaymentReport).filter(BillingPaymentReport.id == report_id).with_for_update().first()
    if not r:
        raise HTTPException(404, "回報不存在")
    if r.status != "pending":
        raise HTTPException(409, "這份回報已處理過")
    _clear_pending(db, r.id)
    r.status, r.reviewed_by, r.reviewed_at, r.review_note = "rejected", me.id, datetime.utcnow(), body.reason
    log_action(db, me, "billing.report.reject", "billing_report", r.id, body.reason)
    db.commit()
    return _report_out(db, r, taipei_today())


# ── reseller 自助 ─────────────────────────────────────────────────────

@router.get("/my/subscriptions")
def my_subscriptions(db: Session = Depends(get_db), me: User = Depends(get_current_user)):
    subs = db.query(BillingSubscriptionV2).filter(BillingSubscriptionV2.customer_id == me.id).order_by(
        BillingSubscriptionV2.id.desc()).all()
    today = taipei_today()
    names = {me.id: me.username}
    lock_map = get_lock_map(db)
    out = [_sub_out(db, s, today, names, lock_map) for s in subs]
    for row in out:
        row.pop("note", None)   # 內部備註不給付款人看
    return out


@router.get("/my/bills")
def my_bills(status: Optional[Literal["unpaid", "paid", "void"]] = None,
             db: Session = Depends(get_db), me: User = Depends(get_current_user)):
    q = db.query(BillingBill).filter(BillingBill.customer_id == me.id)
    if status:
        q = q.filter(BillingBill.status == status)
    bills = q.order_by(BillingBill.due_date.desc(), BillingBill.id.desc()).limit(500).all()
    today = taipei_today()
    statuses = {s.id: s.status for s in db.query(BillingSubscriptionV2).filter(
        BillingSubscriptionV2.id.in_({b.subscription_id for b in bills} or {0})).all()}
    return [_bill_out(b, today, statuses.get(b.subscription_id)) for b in bills]


@router.get("/notices/my")
def my_notices(db: Session = Depends(get_db), me: User = Depends(get_current_user)):
    """登入提示資料（spec §9.2）。非 reseller 一律空，前端就不會跳出對話框。"""
    empty = {"overdue": [], "pending": [], "rejected": [], "overdue_total": 0, "overdue_count": 0,
             "payment_instructions": None}
    if me.role != "reseller":
        return empty
    today = taipei_today()
    unpaid = db.query(BillingBill).filter(BillingBill.customer_id == me.id,
                                          BillingBill.status == "unpaid").all()
    statuses = {s.id: s.status for s in db.query(BillingSubscriptionV2).filter(
        BillingSubscriptionV2.id.in_({b.subscription_id for b in unpaid} or {0})).all()}
    overdue = [_bill_out(b, today, statuses.get(b.subscription_id)) for b in unpaid
               if today > b.due_date and not b.pending_report_id]
    pending = [_bill_out(b, today, statuses.get(b.subscription_id)) for b in unpaid if b.pending_report_id]
    overdue.sort(key=lambda x: -x["overdue_days"])
    since = datetime.utcnow() - timedelta(days=14)
    rejected = [{"id": r.id, "paid_on": _iso(r.paid_on), "amount": r.amount, "reason": r.review_note,
                 "reviewed_at": _utc_iso(r.reviewed_at)}
                for r in db.query(BillingPaymentReport).filter(
                    BillingPaymentReport.customer_id == me.id, BillingPaymentReport.status == "rejected",
                    BillingPaymentReport.reviewed_at >= since).order_by(BillingPaymentReport.id.desc()).all()]
    return {"overdue": overdue, "pending": pending, "rejected": rejected,
            "overdue_total": sum(b["amount"] for b in overdue), "overdue_count": len(overdue),
            "payment_instructions": _get_setting(db, "payment_instructions")}


# ── 總覽與設定 ────────────────────────────────────────────────────────

@router.get("/admin/dashboard")
def dashboard(db: Session = Depends(get_db), _: User = Depends(ADMIN)):
    today = taipei_today()
    unpaid = db.query(BillingBill).filter(BillingBill.status == "unpaid").all()
    overdue = [b for b in unpaid if today > b.due_date]
    month_start = today.replace(day=1)
    next_month = (month_start + timedelta(days=32)).replace(day=1)
    due_this_month = [b for b in unpaid if month_start <= b.due_date < next_month]
    pending = db.query(BillingPaymentReport).filter(BillingPaymentReport.status == "pending").all()
    stale = [r for r in pending if (today - taipei_today(r.created_at)).days > PENDING_ALERT_DAYS]
    subs = db.query(BillingSubscriptionV2).all()
    lock_map = get_lock_map(db)
    groups = _overdue_groups(db, today)
    return {
        "unpaid_total": sum(b.amount for b in unpaid), "unpaid_count": len(unpaid),
        "overdue_total": sum(b.amount for b in overdue), "overdue_count": len(overdue),
        "overdue_undecided": groups["undecided"],
        "due_this_month_total": sum(b.amount for b in due_this_month),
        "due_this_month_count": len(due_this_month),
        "pending_reports": len(pending), "pending_reports_stale": len(stale),
        "active_subscriptions": sum(1 for s in subs if s.status == "active"),
        "scheduled_subscriptions": sum(1 for s in subs if s.status == "scheduled"),
        "suspended_cameras": sum(1 for v in lock_map.values() if v["state"] == "suspended"),
        "ended_locked_cameras": sum(1 for v in lock_map.values() if v["state"] == "ended"),
    }


class SettingsIn(BaseModel):
    payment_instructions: Optional[str] = Field(default=None, max_length=2000)


@router.get("/admin/settings")
def get_settings(db: Session = Depends(get_db), _: User = Depends(ADMIN)):
    return {k: _get_setting(db, k) for k in SETTING_KEYS}


@router.put("/admin/settings")
def put_settings(body: SettingsIn, db: Session = Depends(get_db), me: User = Depends(ADMIN)):
    for k, v in body.model_dump(exclude_unset=True).items():
        row = db.query(BillingSetting).filter(BillingSetting.key == k).first()
        if row:
            row.value = v
        else:
            db.add(BillingSetting(key=k, value=v))
    log_action(db, me, "billing.settings.update", "billing_settings", None, None)
    db.commit()
    return {k: _get_setting(db, k) for k in SETTING_KEYS}
