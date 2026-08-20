"""計費模組 API。

權限原則（spec §6）：
- /billing/admin/* 一律 require_role("symotus_admin")，與前端顯不顯示無關。
- /billing/*/my 只回 current_user 自己的資料，路徑不吃 user_id 參數——
  沒有可竄改的輸入，就沒有 IDOR。
"""
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from models import User, BillingPlan, BillingCustomer, BillingSubscription, BillingInvoice, BillingInvoiceLine
from schemas import (
    PlanCreate, PlanResponse,
    CustomerUpdate, CustomerResponse,
    SubscriptionCreate, SubscriptionResponse,
    InvoiceResponse, InvoiceLineResponse, InvoiceDetailResponse,
)
from auth import require_role
from audit import log_action
from services.billing_calc import invoice_total

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


PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _check_period(period: str):
    if not PERIOD_RE.match(period):
        raise HTTPException(422, "期別格式須為 YYYY-MM")


def _invoice_response(inv: BillingInvoice, customer_name: str | None = None) -> InvoiceResponse:
    return InvoiceResponse(
        id=inv.id, customer_id=inv.customer_id, customer_name=customer_name,
        period=inv.period, total=inv.total, status=inv.status,
        issued_at=inv.issued_at, paid_at=inv.paid_at,
    )


@router.get("/admin/invoices", response_model=list[InvoiceResponse])
def list_invoices(period: str, db: Session = Depends(get_db), current_user: User = Depends(ADMIN)):
    _check_period(period)
    invs = db.query(BillingInvoice).filter(BillingInvoice.period == period).order_by(BillingInvoice.id).all()
    names = {u.id: u.username for u in db.query(User).all()}
    return [_invoice_response(i, names.get(i.customer_id)) for i in invs]


@router.get("/admin/invoices/{invoice_id}", response_model=InvoiceDetailResponse)
def get_invoice(invoice_id: int, db: Session = Depends(get_db), current_user: User = Depends(ADMIN)):
    inv = db.query(BillingInvoice).filter(BillingInvoice.id == invoice_id).first()
    if not inv:
        raise HTTPException(404, "發票不存在")
    return _build_invoice_detail(db, inv)


def _build_invoice_detail(db: Session, inv: BillingInvoice) -> InvoiceDetailResponse:
    lines = db.query(BillingInvoiceLine).filter(BillingInvoiceLine.invoice_id == inv.id).all()
    u = db.query(User).filter(User.id == inv.customer_id).first()
    return InvoiceDetailResponse(
        id=inv.id, customer_id=inv.customer_id, customer_name=u.username if u else None,
        period=inv.period, total=inv.total, status=inv.status,
        issued_at=inv.issued_at, paid_at=inv.paid_at,
        lines=[InvoiceLineResponse(id=l.id, camera_id=l.camera_id, camera_name=l.camera_name,
                                   plan_name=l.plan_name, amount=l.amount) for l in lines],
    )


@router.post("/admin/invoices/generate/{period}")
def generate_invoices(period: str, db: Session = Depends(get_db), current_user: User = Depends(ADMIN)):
    """為該期別產生發票。冪等：已有發票的客戶直接跳過。

    冪等性靠 UNIQUE(customer_id, period)＋這裡的預先查詢兩層保障。
    舊版沒有這層，管理者點兩次就產生兩批發票。
    """
    _check_period(period)
    existing = {i.customer_id for i in db.query(BillingInvoice).filter(BillingInvoice.period == period).all()}

    subs = db.query(BillingSubscription).filter(BillingSubscription.status == "active").all()
    plans = {p.id: p for p in db.query(BillingPlan).all()}

    by_customer: dict[int, list] = {}
    for s in subs:
        by_customer.setdefault(s.customer_id, []).append(s)

    created = 0
    for customer_id, customer_subs in by_customer.items():
        if customer_id in existing:
            continue
        line_data = []
        for s in customer_subs:
            plan = plans.get(s.plan_id)
            if not plan:
                continue
            line_data.append((s, plan))
        if not line_data:
            continue

        inv = BillingInvoice(
            customer_id=customer_id, period=period,
            total=invoice_total([p.monthly_fee for _, p in line_data]),
            status="unpaid",
        )
        db.add(inv); db.flush()   # 取得 inv.id 供明細使用
        for s, p in line_data:
            # 快照：方案改價或相機改名之後，這張發票的內容不得跟著變
            db.add(BillingInvoiceLine(
                invoice_id=inv.id, subscription_id=s.id, camera_id=s.camera_id,
                camera_name=None, plan_name=p.name, amount=p.monthly_fee,
            ))
        created += 1

    log_action(db, current_user, "billing_generate_invoices", "billing_invoice", None,
               f"period={period} created={created}")
    db.commit()
    return {"message": f"{period} 已產生 {created} 張發票", "created": created}


@router.post("/admin/invoices/{invoice_id}/mark-paid")
def mark_invoice_paid(invoice_id: int, db: Session = Depends(get_db), current_user: User = Depends(ADMIN)):
    inv = db.query(BillingInvoice).filter(BillingInvoice.id == invoice_id).first()
    if not inv:
        raise HTTPException(404, "發票不存在")
    if inv.status == "void":
        raise HTTPException(409, "已作廢的發票不能標記收款")
    inv.status = "paid"
    inv.paid_at = datetime.utcnow()
    log_action(db, current_user, "billing_mark_paid", "billing_invoice", invoice_id)
    db.commit()
    return {"message": "已標記收款"}


@router.post("/admin/invoices/{invoice_id}/void")
def void_invoice(invoice_id: int, db: Session = Depends(get_db), current_user: User = Depends(ADMIN)):
    inv = db.query(BillingInvoice).filter(BillingInvoice.id == invoice_id).first()
    if not inv:
        raise HTTPException(404, "發票不存在")
    inv.status = "void"
    log_action(db, current_user, "billing_void_invoice", "billing_invoice", invoice_id)
    db.commit()
    return {"message": "已作廢"}


@router.get("/admin/dashboard")
def billing_dashboard(period: str, db: Session = Depends(get_db), current_user: User = Depends(ADMIN)):
    _check_period(period)
    invs = db.query(BillingInvoice).filter(BillingInvoice.period == period).all()
    frozen = db.query(BillingCustomer).filter(BillingCustomer.frozen == True).count()  # noqa: E712
    return {
        "period": period,
        "invoice_count": len(invs),
        "total_billed": sum(i.total for i in invs if i.status != "void"),
        "total_unpaid": sum(i.total for i in invs if i.status == "unpaid"),
        "total_paid": sum(i.total for i in invs if i.status == "paid"),
        "frozen_count": frozen,
    }
