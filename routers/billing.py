"""計費模組 API。

權限原則（spec §6）：
- /billing/admin/* 一律 require_role("symotus_admin")，與前端顯不顯示無關。
- /billing/*/my 只回 current_user 自己的資料，路徑不吃 user_id 參數——
  沒有可竄改的輸入，就沒有 IDOR。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from models import User, BillingPlan
from schemas import PlanCreate, PlanResponse
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
