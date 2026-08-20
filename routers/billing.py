"""計費模組 API。

權限原則（spec §6）：
- /billing/admin/* 一律 require_role("symotus_admin")，與前端顯不顯示無關。
- /billing/*/my 只回 current_user 自己的資料，路徑不吃 user_id 參數——
  沒有可竄改的輸入，就沒有 IDOR。
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from models import User, BillingPlan, BillingCustomer, BillingSubscription
from schemas import (
    PlanCreate, PlanResponse,
    CustomerUpdate, CustomerResponse,
    SubscriptionCreate, SubscriptionResponse,
)
from auth import require_role
from audit import log_action

router = APIRouter(prefix="/billing", tags=["billing"])

ADMIN = require_role("symotus_admin")


@router.get("/admin/plans", response_model=list[PlanResponse])
def list_plans(
    include_inactive: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(ADMIN),
):
    q = db.query(BillingPlan)
    if not include_inactive:
        q = q.filter(BillingPlan.is_active == True)  # noqa: E712
    return q.order_by(BillingPlan.id).all()


@router.post("/admin/plans", response_model=PlanResponse)
def create_plan(
    body: PlanCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(ADMIN),
):
    plan = BillingPlan(**body.model_dump())
    db.add(plan)
    log_action(db, current_user, "billing_create_plan", "billing_plan", None, f"name={body.name}")
    db.commit(); db.refresh(plan)
    return plan


@router.put("/admin/plans/{plan_id}", response_model=PlanResponse)
def update_plan(
    plan_id: int,
    body: PlanCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(ADMIN),
):
    plan = db.query(BillingPlan).filter(BillingPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(404, "方案不存在")
    for k, v in body.model_dump().items():
        setattr(plan, k, v)
    # 已開立的發票存的是快照，不受這次改價影響（spec §4）
    log_action(db, current_user, "billing_update_plan", "billing_plan", plan_id, f"fee={body.monthly_fee}")
    db.commit(); db.refresh(plan)
    return plan


@router.delete("/admin/plans/{plan_id}")
def deactivate_plan(
    plan_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(ADMIN),
):
    """軟刪。真刪會讓發票明細指向不存在的方案。"""
    plan = db.query(BillingPlan).filter(BillingPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(404, "方案不存在")
    plan.is_active = False
    log_action(db, current_user, "billing_deactivate_plan", "billing_plan", plan_id)
    db.commit()
    return {"message": "已停用方案"}


def get_or_create_customer(db: Session, user_id: int) -> BillingCustomer:
    """計費設定採 lazy 建立：使用者建立時不必知道計費模組的存在。"""
    c = db.query(BillingCustomer).filter(BillingCustomer.user_id == user_id).first()
    if not c:
        c = BillingCustomer(user_id=user_id)
        db.add(c)
        try:
            db.commit()
        except IntegrityError:
            # 並發下可能有另一個請求搶先建立同一筆客戶紀錄；
            # user_id 是主鍵不會產生重複列，rollback 後重查回傳既有那筆即可
            db.rollback()
            c = db.query(BillingCustomer).filter(BillingCustomer.user_id == user_id).first()
        else:
            db.refresh(c)
    return c


@router.get("/admin/customers", response_model=list[CustomerResponse])
def list_customers(
    role: str = "all",
    db: Session = Depends(get_db),
    current_user: User = Depends(ADMIN),
):
    q = db.query(User).filter(User.is_active == True)  # noqa: E712
    if role != "all":
        q = q.filter(User.role == role)
    out = []
    for u in q.order_by(User.id).all():
        c = get_or_create_customer(db, u.id)
        out.append(CustomerResponse(
            user_id=u.id, username=u.username, email=u.email, role=u.role,
            billing_day=c.billing_day, frozen=c.frozen, note=c.note,
        ))
    return out


@router.put("/admin/customers/{user_id}", response_model=CustomerResponse)
def update_customer(
    user_id: int,
    body: CustomerUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(ADMIN),
):
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(404, "使用者不存在")
    c = get_or_create_customer(db, user_id)
    if body.billing_day is not None:
        c.billing_day = body.billing_day
    if body.note is not None:
        c.note = body.note
    log_action(db, current_user, "billing_update_customer", "billing_customer", user_id)
    db.commit(); db.refresh(c)
    return CustomerResponse(user_id=u.id, username=u.username, email=u.email, role=u.role,
                            billing_day=c.billing_day, frozen=c.frozen, note=c.note)


@router.post("/admin/customers/{user_id}/freeze")
def freeze_customer(user_id: int, db: Session = Depends(get_db), current_user: User = Depends(ADMIN)):
    """只做標記：不會自動擋登入或斷服務（spec §5）。"""
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(404, "使用者不存在")
    c = get_or_create_customer(db, user_id)
    c.frozen = True
    c.frozen_at = datetime.utcnow()
    log_action(db, current_user, "billing_freeze_customer", "billing_customer", user_id)
    db.commit()
    return {"message": "已標記凍結"}


@router.post("/admin/customers/{user_id}/unfreeze")
def unfreeze_customer(user_id: int, db: Session = Depends(get_db), current_user: User = Depends(ADMIN)):
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(404, "使用者不存在")
    c = get_or_create_customer(db, user_id)
    c.frozen = False
    c.frozen_at = None
    log_action(db, current_user, "billing_unfreeze_customer", "billing_customer", user_id)
    db.commit()
    return {"message": "已解除凍結"}


def _sub_response(sub: BillingSubscription, plan: BillingPlan | None) -> SubscriptionResponse:
    return SubscriptionResponse(
        id=sub.id, camera_id=sub.camera_id, customer_id=sub.customer_id, plan_id=sub.plan_id,
        plan_name=plan.name if plan else None, monthly_fee=plan.monthly_fee if plan else 0,
        status=sub.status,
    )


@router.get("/admin/subscriptions", response_model=list[SubscriptionResponse])
def list_subscriptions(db: Session = Depends(get_db), current_user: User = Depends(ADMIN)):
    subs = db.query(BillingSubscription).order_by(BillingSubscription.id).all()
    plans = {p.id: p for p in db.query(BillingPlan).all()}
    return [_sub_response(s, plans.get(s.plan_id)) for s in subs]


@router.post("/admin/subscriptions", response_model=SubscriptionResponse)
def create_subscription(
    body: SubscriptionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(ADMIN),
):
    plan = db.query(BillingPlan).filter(BillingPlan.id == body.plan_id).first()
    if not plan:
        raise HTTPException(404, "方案不存在")
    if not db.query(User).filter(User.id == body.customer_id).first():
        raise HTTPException(404, "客戶不存在")
    dup = db.query(BillingSubscription).filter(
        BillingSubscription.camera_id == body.camera_id,
        BillingSubscription.status == "active",
    ).first()
    if dup:
        raise HTTPException(409, "此相機已有生效中的訂閱")

    sub = BillingSubscription(camera_id=body.camera_id, customer_id=body.customer_id, plan_id=body.plan_id)
    db.add(sub)
    log_action(db, current_user, "billing_create_subscription", "billing_subscription", None,
               f"camera={body.camera_id} plan={plan.name}")
    db.commit(); db.refresh(sub)
    return _sub_response(sub, plan)


@router.delete("/admin/subscriptions/{sub_id}")
def cancel_subscription(sub_id: int, db: Session = Depends(get_db), current_user: User = Depends(ADMIN)):
    """軟性取消：保留歷史，讓已開立的發票仍能追溯到來源訂閱。"""
    sub = db.query(BillingSubscription).filter(BillingSubscription.id == sub_id).first()
    if not sub:
        raise HTTPException(404, "訂閱不存在")
    sub.status = "cancelled"
    sub.cancelled_at = datetime.utcnow()
    log_action(db, current_user, "billing_cancel_subscription", "billing_subscription", sub_id)
    db.commit()
    return {"message": "已取消訂閱"}
