"""計費模組 API。

權限原則（spec §6）：
- /billing/admin/* 一律 require_role("symotus_admin")，與前端顯不顯示無關。
- /billing/*/my 只回 current_user 自己的資料，路徑不吃 user_id 參數——
  沒有可竄改的輸入，就沒有 IDOR。
"""
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from models import (
    User, BillingPlan, BillingCustomer, BillingSubscription, BillingInvoice, BillingInvoiceLine,
    BillingUsageDaily,
)
from schemas import (
    PlanCreate, PlanUpdate, PlanResponse,
    CustomerUpdate, CustomerResponse,
    SubscriptionCreate, SubscriptionResponse,
    InvoiceResponse, InvoiceLineResponse, InvoiceDetailResponse,
    CommissionRowResponse,
    MySubscriptionResponse, MyQuotaResponse,
)
from auth import require_role, get_current_user
from audit import log_action
from services.billing_calc import (
    invoice_total, quota_state, period_of, period_bounds_utc, effective_monthly_fee,
    commission_amount, commission_display,
)
from services.billing_usage import run_collection

router = APIRouter(prefix="/billing", tags=["billing"])

ADMIN = require_role("symotus_admin")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@router.get("/admin/plans", response_model=list[PlanResponse])
def list_plans(
    include_inactive: bool = False,
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
    body: PlanUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(ADMIN),
):
    plan = db.query(BillingPlan).filter(BillingPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(404, "方案不存在")
    # 局部更新：只套用請求裡「真的有帶」的欄位，未帶的欄位維持既有值
    # （例如只傳 {"is_active": true} 不該把 monthly_fee/quota 打歸零）。
    for k, v in body.model_dump(exclude={"is_active"}, exclude_unset=True).items():
        setattr(plan, k, v)
    if body.is_active is not None:
        # 允許重新啟用已停用的方案；is_active 未帶值時不動它（PlanCreate 沒有這欄，舊呼叫端仍相容）
        plan.is_active = body.is_active
    # 已開立的發票存的是快照，不受這次改價影響（spec §4）
    log_action(db, current_user, "billing_update_plan", "billing_plan", plan_id, f"fee={plan.monthly_fee}")
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
    # 這是 GET，不該有寫入副作用：list_customers 曾經對每個使用者呼叫
    # get_or_create_customer（內含 commit），N 個使用者 = N 次 insert + N 次 commit，
    # 且與所有相機 CRUD proxy 共用連線池。改用一次查完 billing_customers 再用
    # Python dict 比對，沒有紀錄的使用者用預設值組出回應，完全不寫 DB。
    q = db.query(User).filter(User.is_active == True)  # noqa: E712
    if role != "all":
        q = q.filter(User.role == role)
    users = q.order_by(User.id).all()
    customers = {c.user_id: c for c in db.query(BillingCustomer).all()}
    out = []
    for u in users:
        c = customers.get(u.id)
        out.append(CustomerResponse(
            user_id=u.id, username=u.username, email=u.email, role=u.role,
            billing_day=c.billing_day if c else 1,
            frozen=c.frozen if c else False,
            note=c.note if c else None,
            payment_method=c.payment_method if c else "monthly_transfer",
            statement_day=c.statement_day if c else 1,
            custom_monthly_fee=c.custom_monthly_fee if c else None,
            commission_type=c.commission_type if c else None,
            commission_percent_bps=c.commission_percent_bps if c else None,
            commission_fixed_amount=c.commission_fixed_amount if c else None,
            commission_display=commission_display(
                c.commission_type, c.commission_percent_bps, c.commission_fixed_amount
            ) if c else "",
        ))
    return out


# NOT NULL 欄位：models.py 裡 nullable=False。顯式傳 null 會讓 setattr 把欄位
# 寫成 None，直接違反 DB 的 NOT NULL 約束（而且是在 commit 當下才炸，訊息不友善）。
# 這裡提前擋下，回一個看得懂原因的 422。
_NOT_NULL_CUSTOMER_FIELDS = {"billing_day", "payment_method", "statement_day"}


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
    # PATCH 語意：只套用請求中真的有帶的欄位，避免把沒帶到的欄位覆寫成 None
    # （custom_monthly_fee 等欄位 0 是合法值，不能用 is not None 判斷）。
    # 用 model_fields_set 而非 model_dump(exclude_unset=True) 的 key 集合：
    # 兩者理論上一致，但這裡明確表達「有沒有帶這個 key」的判斷依據。
    fields_set = body.model_fields_set
    data = body.model_dump(exclude_unset=True)
    for field in fields_set & _NOT_NULL_CUSTOMER_FIELDS:
        if data.get(field) is None:
            raise HTTPException(422, f"{field} 不可清空為 null（此欄位不允許為空）")
    # 只送數值欄位（commission_percent_bps / commission_fixed_amount）而不送
    # commission_type 時，schema 層看不到客戶目前已存的型別，這裡用客戶現有的
    # commission_type 判斷數值有沒有送錯欄位——避免看似合法的單一欄位更新，
    # 實際上把數值寫進與目前型別不符的欄位（單位錯亂）。
    if "commission_type" not in fields_set:
        if "commission_percent_bps" in fields_set and c.commission_type != "percent":
            raise HTTPException(422, "未設定 commission_type 為 percent 時，不可更新 commission_percent_bps")
        if "commission_fixed_amount" in fields_set and c.commission_type != "fixed":
            raise HTTPException(422, "未設定 commission_type 為 fixed 時，不可更新 commission_fixed_amount")
        # 型別本身沒被清除（沒帶 commission_type）的情況下，數值欄位被明確傳 null
        # 等於想清掉數值卻留著型別，型別/數值會不成對。要清空分潤設定必須整組清，
        # 也就是傳 {"commission_type": null}，而不是單獨把數值清成 null。
        if "commission_percent_bps" in fields_set and data.get("commission_percent_bps") is None:
            raise HTTPException(422, "不可單獨將 commission_percent_bps 清空為 null；"
                                      "請傳 {\"commission_type\": null} 清除整組分潤設定")
        if "commission_fixed_amount" in fields_set and data.get("commission_fixed_amount") is None:
            raise HTTPException(422, "不可單獨將 commission_fixed_amount 清空為 null；"
                                      "請傳 {\"commission_type\": null} 清除整組分潤設定")
    for field in ("billing_day", "note", "payment_method", "statement_day",
                  "custom_monthly_fee", "commission_type",
                  "commission_percent_bps", "commission_fixed_amount"):
        if field in data:
            setattr(c, field, data[field])
    # 型別變更時把不相符的數值欄位清掉：兩個欄位都會回傳給前端，
    # 殘留值會讓管理者看到「型別是固定金額，卻同時顯示 15%」。
    if "commission_type" in fields_set:
        if c.commission_type != "percent":
            c.commission_percent_bps = None
        if c.commission_type != "fixed":
            c.commission_fixed_amount = None
    # 稽核日誌只在分潤欄位真的有被這次請求改動時才附上分潤顯示字串，
    # 避免日誌內容誤導成「這次操作有動到分潤」——即使值沒變。
    commission_changed = bool(
        fields_set & {"commission_type", "commission_percent_bps", "commission_fixed_amount"}
    )
    detail = commission_display(
        c.commission_type, c.commission_percent_bps, c.commission_fixed_amount
    ) if commission_changed else ""
    log_action(db, current_user, "billing_update_customer", "billing_customer", user_id, detail)
    db.commit(); db.refresh(c)
    return CustomerResponse(user_id=u.id, username=u.username, email=u.email, role=u.role,
                            billing_day=c.billing_day, frozen=c.frozen, note=c.note,
                            payment_method=c.payment_method, statement_day=c.statement_day,
                            custom_monthly_fee=c.custom_monthly_fee,
                            commission_type=c.commission_type,
                            commission_percent_bps=c.commission_percent_bps,
                            commission_fixed_amount=c.commission_fixed_amount,
                            commission_display=commission_display(
                                c.commission_type, c.commission_percent_bps, c.commission_fixed_amount
                            ))


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


def _sub_response(sub: BillingSubscription, plan: BillingPlan | None,
                  customer: BillingCustomer | None) -> SubscriptionResponse:
    # monthly_fee 要顯示「客戶實付」，不是方案原價——自訂月費（custom_monthly_fee）
    # 設了就覆蓋方案月費，這裡跟 generate_invoices 用同一個 effective_monthly_fee，
    # 否則列表上看到的月費會跟真正開出來的發票金額對不上。
    plan_fee = plan.monthly_fee if plan else 0
    custom_fee = customer.custom_monthly_fee if customer else None
    return SubscriptionResponse(
        id=sub.id, camera_id=sub.camera_id, customer_id=sub.customer_id, plan_id=sub.plan_id,
        plan_name=plan.name if plan else None,
        monthly_fee=effective_monthly_fee(plan_fee, custom_fee) if plan else 0,
        status=sub.status,
    )


@router.get("/admin/subscriptions", response_model=list[SubscriptionResponse])
def list_subscriptions(db: Session = Depends(get_db), current_user: User = Depends(ADMIN)):
    subs = db.query(BillingSubscription).order_by(BillingSubscription.id).all()
    plans = {p.id: p for p in db.query(BillingPlan).all()}
    # 一次撈出所有客戶的自訂月費設定，避免逐筆查 DB（同檔既有慣例）。
    customer_ids = {s.customer_id for s in subs}
    customer_configs = {
        c.user_id: c for c in db.query(BillingCustomer).filter(BillingCustomer.user_id.in_(customer_ids)).all()
    }
    return [_sub_response(s, plans.get(s.plan_id), customer_configs.get(s.customer_id)) for s in subs]


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

    # camera_serial 故意不從任何來源帶入，永遠是新建時的預設值 None：
    # 目前沒有「更改既有訂閱 camera_id」的端點，所以沒有快取失效路徑；
    # 未來若加上相機改派功能，務必在那裡清空舊訂閱的 camera_serial。
    sub = BillingSubscription(camera_id=body.camera_id, customer_id=body.customer_id, plan_id=body.plan_id)
    db.add(sub)
    log_action(db, current_user, "billing_create_subscription", "billing_subscription", None,
               f"camera={body.camera_id} plan={plan.name}")
    try:
        db.commit()
    except IntegrityError:
        # 並發下兩個請求可能都通過上面的 SELECT 檢查，部分唯一索引是最後防線
        db.rollback()
        raise HTTPException(409, "此相機已有生效中的訂閱")
    db.refresh(sub)
    customer = db.query(BillingCustomer).filter(BillingCustomer.user_id == body.customer_id).first()
    return _sub_response(sub, plan, customer)


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
    customer_ids = {i.customer_id for i in invs}
    names = {u.id: u.username for u in db.query(User).filter(User.id.in_(customer_ids)).all()}
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
    # 只計入非作廢的發票：作廢的發票不再佔用該期別，讓客戶＋期別可以重新開立
    existing = {
        i.customer_id for i in db.query(BillingInvoice).filter(
            BillingInvoice.period == period, BillingInvoice.status != "void",
        ).all()
    }

    period_start, _period_end = period_bounds_utc(period)
    # design doc §5：入會當月免費，帳單從「加入後的下一期」才開始收——
    # 訂閱必須在該期別開始「之前」就已存在(started_at < period_start)。
    # 且訂閱狀態不能只看 active：已取消的訂閱在取消之前仍佔用過的期別，
    # 也應該能補開歷史發票，所以連 cancelled 一起查，改用
    # cancelled_at IS NULL OR cancelled_at > period_start 判斷「該期別當時是否仍生效」。
    # 用嚴格大於（而非 >=）是為了與 started_at < period_start 對稱：剛好在期別起點
    # 取消，代表這個客戶在該期別「一刻都沒有生效過」，不該被計費，等同剛好在期別
    # 起點才加入的訂閱該期免費一樣的道理。
    subs = db.query(BillingSubscription).filter(
        BillingSubscription.status.in_(["active", "cancelled"]),
        BillingSubscription.started_at < period_start,
        (BillingSubscription.cancelled_at.is_(None)) | (BillingSubscription.cancelled_at > period_start),
    ).all()
    plans = {p.id: p for p in db.query(BillingPlan).all()}

    by_customer: dict[int, list] = {}
    for s in subs:
        by_customer.setdefault(s.customer_id, []).append(s)

    # 一次撈出所有相關客戶的自訂月費設定，避免在下方迴圈內逐一查 DB
    # （同檔 list_subscriptions／list_customers 的既有慣例）。
    customer_configs = {
        c.user_id: c for c in db.query(BillingCustomer).filter(
            BillingCustomer.user_id.in_(by_customer.keys())
        ).all()
    }

    created = 0
    skipped_existing = 0  # 冪等跳過：這期已有非作廢發票，正常重跑行為，不是錯誤
    skipped_conflict = 0  # 真的撞到部分唯一索引：預查沒擋到、寫入當下才發現的並發衝突
    for customer_id, customer_subs in by_customer.items():
        if customer_id in existing:
            skipped_existing += 1
            continue
        line_data = []
        for s in customer_subs:
            plan = plans.get(s.plan_id)
            if not plan:
                continue
            line_data.append((s, plan))
        if not line_data:
            continue

        # 自訂月費覆蓋方案月費：custom_monthly_fee 為 None 代表沒談成客製價，
        # 用方案原價；0 是合法值（談成免費），effective_monthly_fee 已處理這個判斷。
        cfg = customer_configs.get(customer_id)
        custom_fee = cfg.custom_monthly_fee if cfg else None
        line_fees = [(s, p, effective_monthly_fee(p.monthly_fee, custom_fee)) for s, p in line_data]

        # 每個客戶各自一個 savepoint：舊版整批只 commit 一次，
        # 若某個客戶觸發部分唯一索引，rollback 會連同其他客戶「這次呼叫已建立
        # 但尚未提交」的發票與明細一起丟掉，卻仍回報成功。改成逐客戶 savepoint，
        # 一個客戶失敗只影響該客戶，其餘客戶正常寫入。
        try:
            with db.begin_nested():
                inv = BillingInvoice(
                    customer_id=customer_id, period=period,
                    total=invoice_total([fee for _, _, fee in line_fees]),
                    status="unpaid",
                )
                db.add(inv); db.flush()   # 取得 inv.id 供明細使用
                for s, p, fee in line_fees:
                    # 快照：方案改價、相機改名、或事後調整自訂月費，
                    # 都不得影響這張已開立發票的明細金額。
                    db.add(BillingInvoiceLine(
                        invoice_id=inv.id, subscription_id=s.id, camera_id=s.camera_id,
                        camera_name=None, plan_name=p.name, amount=fee,
                    ))
        except IntegrityError:
            # 並發下另一個管理者同時產生了同一客戶＋期別的發票，
            # 部分唯一索引擋下重複列；savepoint 已自動 rollback，計入 skipped_conflict
            skipped_conflict += 1
            continue
        created += 1

    skipped = skipped_existing + skipped_conflict
    log_action(db, current_user, "billing_generate_invoices", "billing_invoice", None,
               f"period={period} created={created} skipped_existing={skipped_existing} "
               f"skipped_conflict={skipped_conflict}")
    db.commit()
    msg = f"{period} 已產生 {created} 張發票"
    if skipped_existing:
        msg += f"，{skipped_existing} 筆該客戶本期已有發票故跳過"
    if skipped_conflict:
        msg += f"，另有 {skipped_conflict} 筆因併發衝突略過"
    return {
        "message": msg,
        "created": created,
        "skipped": skipped,
        "skipped_existing": skipped_existing,
        "skipped_conflict": skipped_conflict,
    }


@router.post("/admin/invoices/{invoice_id}/mark-paid")
def mark_invoice_paid(invoice_id: int, db: Session = Depends(get_db), current_user: User = Depends(ADMIN)):
    inv = db.query(BillingInvoice).filter(BillingInvoice.id == invoice_id).first()
    if not inv:
        raise HTTPException(404, "發票不存在")
    if inv.status == "void":
        raise HTTPException(409, "已作廢的發票不能標記收款")
    if inv.status == "paid":
        # 重複點擊不是錯誤操作，但不能覆寫已存在的收款時間，
        # 否則帳上的收款時點會被重複點擊不斷往後推，變得不可信
        return {"message": "此發票已標記收款，未變更收款時間"}
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
    if inv.status == "paid":
        raise HTTPException(409, "已收款的發票須先辦理退款/沖銷才能作廢")
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
        # 排除 void：作廢後重開屬正常流程，張數統計不應把作廢的舊發票也算進去
        "invoice_count": len([i for i in invs if i.status != "void"]),
        "total_billed": sum(i.total for i in invs if i.status != "void"),
        "total_unpaid": sum(i.total for i in invs if i.status == "unpaid"),
        "total_paid": sum(i.total for i in invs if i.status == "paid"),
        "frozen_count": frozen,
    }


@router.get("/admin/commissions", response_model=list[CommissionRowResponse])
def list_commissions(period: str, db: Session = Depends(get_db), current_user: User = Depends(ADMIN)):
    """分潤應付報表。

    分潤不進發票（設計決定）：發票照定價開給客戶，這張報表算的是
    「我這期要付給各經銷商多少」。基數是該期別**非作廢**發票的總額。
    只列有設分潤條件的客戶——沒設的列出來只是雜訊。
    """
    _check_period(period)

    # 一次撈出該期別所有非 void 發票，依 customer_id 加總成 dict，避免逐客戶查詢。
    invs = db.query(BillingInvoice).filter(
        BillingInvoice.period == period, BillingInvoice.status != "void",
    ).all()
    totals: dict[int, int] = {}
    for i in invs:
        totals[i.customer_id] = totals.get(i.customer_id, 0) + i.total

    # 一次撈出所有設有分潤條件的客戶（commission_type 非 NULL）。
    customers = db.query(BillingCustomer).filter(BillingCustomer.commission_type.isnot(None)).all()

    # 使用者名稱一次查完，不逐筆查 DB。
    names = {u.id: u.username for u in db.query(User).filter(
        User.id.in_([c.user_id for c in customers])
    ).all()}

    out = []
    for c in customers:
        base = totals.get(c.user_id, 0)
        amount = commission_amount(base, c.commission_type, c.commission_percent_bps, c.commission_fixed_amount)
        out.append(CommissionRowResponse(
            customer_id=c.user_id, customer_name=names.get(c.user_id),
            period=period, invoice_total=base,
            commission_type=c.commission_type,
            commission_percent_bps=c.commission_percent_bps,
            commission_fixed_amount=c.commission_fixed_amount,
            commission_amount=amount,
            commission_display=commission_display(
                c.commission_type, c.commission_percent_bps, c.commission_fixed_amount
            ),
        ))
    return out


# ── 經銷商自助端點（spec §6：/billing/*/my 只回 current_user 自己的資料） ──

@router.get("/subscriptions/my", response_model=list[MySubscriptionResponse])
def my_subscriptions(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    subs = db.query(BillingSubscription).filter(
        BillingSubscription.customer_id == current_user.id,
        BillingSubscription.status == "active",
    ).order_by(BillingSubscription.id).all()
    plans = {p.id: p for p in db.query(BillingPlan).all()}
    # 自訂月費要覆蓋方案原價（同 _sub_response／generate_invoices 慣例），
    # 只有 current_user 自己一筆，不需要批次查（也沒有 N+1 的問題）。
    customer = db.query(BillingCustomer).filter(BillingCustomer.user_id == current_user.id).first()
    custom_fee = customer.custom_monthly_fee if customer else None
    return [
        MySubscriptionResponse(
            id=s.id, camera_id=s.camera_id,
            plan_name=plans[s.plan_id].name if s.plan_id in plans else None,
            monthly_fee=effective_monthly_fee(plans[s.plan_id].monthly_fee, custom_fee) if s.plan_id in plans else 0,
            status=s.status,
        ) for s in subs
    ]


@router.get("/invoices/my", response_model=list[InvoiceResponse])
def my_invoices(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    invs = db.query(BillingInvoice).filter(
        BillingInvoice.customer_id == current_user.id,
    ).order_by(BillingInvoice.period.desc()).all()
    return [_invoice_response(i, current_user.username) for i in invs]


@router.get("/quotas/my", response_model=list[MyQuotaResponse])
def my_quotas(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """本期各相機用量 vs 配額。用量表在階段三才有資料，在此之前一律為 0。"""
    period = period_of(datetime.utcnow())
    subs = db.query(BillingSubscription).filter(
        BillingSubscription.customer_id == current_user.id,
        BillingSubscription.status == "active",
    ).all()
    plans = {p.id: p for p in db.query(BillingPlan).all()}

    out = []
    for s in subs:
        plan = plans.get(s.plan_id)
        tl_used = int(db.query(
            func.coalesce(func.sum(BillingUsageDaily.timelapse_secs), 0),
        ).filter(
            BillingUsageDaily.camera_id == s.camera_id,
            BillingUsageDaily.date.like(f"{period}-%"),
        ).scalar())
        # storage_gb 是時間點快照，不可跨天加總——取期間內最新一列的值，
        # 沒有資料就是 0（同 collect_storage_gb / models.py 欄位註解）。
        last_row = db.query(BillingUsageDaily).filter(
            BillingUsageDaily.camera_id == s.camera_id,
            BillingUsageDaily.date.like(f"{period}-%"),
        ).order_by(BillingUsageDaily.date.desc()).first()
        st_used = float(last_row.storage_gb) if last_row else 0.0
        tl_total = plan.timelapse_quota_secs if plan else 0
        st_total = plan.storage_quota_gb if plan else 0
        # 兩種配額取較嚴重的狀態
        states = [quota_state(tl_used, tl_total), quota_state(st_used, st_total)]
        state = "suspended" if "suspended" in states else ("warned" if "warned" in states else "ok")
        out.append(MyQuotaResponse(
            camera_id=s.camera_id, period=period,
            timelapse_used_secs=tl_used, timelapse_total_secs=tl_total,
            storage_used_gb=st_used, storage_total_gb=st_total, state=state,
        ))
    return out


# 注意：這條必須放在 /invoices/my 之後定義，否則 "my" 會被當成 invoice_id 而回 422
@router.get("/invoices/{invoice_id}", response_model=InvoiceDetailResponse)
def my_invoice_detail(invoice_id: int, db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    inv = db.query(BillingInvoice).filter(BillingInvoice.id == invoice_id).first()
    # 非自己的發票一律回 404（不是 403）：不洩漏「這張發票存在」這個資訊
    if not inv or (inv.customer_id != current_user.id and current_user.role != "symotus_admin"):
        raise HTTPException(404, "發票不存在")
    return _build_invoice_detail(db, inv)


@router.post("/admin/usage/collect")
async def collect_usage(date: str, db: Session = Depends(get_db), current_user: User = Depends(ADMIN)):
    """手動補跑某日的用量採集。每日排程失敗時的補救管道。"""
    if not DATE_RE.match(date):
        raise HTTPException(422, "日期格式須為 YYYY-MM-DD")
    result = await run_collection(db, date)
    log_action(db, current_user, "billing_collect_usage", "billing_usage", None,
               f"date={date} ok={result['cameras']} failed={result['failed']} unresolved={result['unresolved']}")
    db.commit()
    return result
