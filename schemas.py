from pydantic import BaseModel, EmailStr, PlainSerializer, Field, model_validator
from typing import Literal, Optional, List
from typing_extensions import Annotated
from datetime import datetime, timezone

from services.billing_calc import MAX_COMMISSION_BPS, MAX_COMMISSION_FIXED


def utc_iso(v: Optional[datetime]) -> Optional[str]:
    """DB 內的 datetime 皆為 naive UTC（datetime.utcnow）。序列化時補上 UTC 時區，
    輸出帶 offset 的 ISO 字串（例：2026-07-03T15:30:00+00:00），前端 new Date() 才能
    正確解析為絕對時刻並轉台北時間；否則無 offset 會被當成瀏覽器本地時間，慢 8 小時。"""
    if v is None:
        return None
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.isoformat()


# 回應用 datetime：一律輸出帶時區 offset 的 ISO 字串
UtcDatetime = Annotated[datetime, PlainSerializer(utc_iso, when_used="json")]


# ── Auth ──────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds
    # Camera Backend token（由 Auth Service 向 Camera Backend 換取）
    camera_access_token: Optional[str] = None
    camera_refresh_token: Optional[str] = None

class RefreshRequest(BaseModel):
    refresh_token: str

class OAuthCallbackRequest(BaseModel):
    code: str
    state: str
    invite_token: Optional[str] = None  # 從邀請頁過來時帶上


# ── User ──────────────────────────────────────────
class UserCreate(BaseModel):
    username: str
    email: EmailStr
    full_name: Optional[str] = None
    password: str

class UserResponse(BaseModel):
    id: int
    username: str
    email: str
    full_name: Optional[str]
    role: str
    is_active: bool
    reseller_id: Optional[int]
    google_id: Optional[str]
    line_id: Optional[str]
    created_at: UtcDatetime

    class Config:
        from_attributes = True

class UserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    full_name: Optional[str] = None
    is_active: Optional[bool] = None


class AdminUserCreate(BaseModel):
    """symotus_admin 直接後台建帳（POST /admin/users）。"""
    username: str
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: Optional[str] = None
    role: Literal["symotus_admin", "reseller", "end_user"]
    camera_email: Optional[str] = None
    reseller_id: Optional[int] = None


class ResellerUserCreate(BaseModel):
    """reseller 直接建 end_user（POST /users）。刻意不收 role/reseller_id——
    後端一律強制 role=end_user、reseller_id=current_user.id，避免 body 夾帶越權。"""
    username: str
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: Optional[str] = None


# ── Invite ──────────────────────────────────────────
class InviteCreate(BaseModel):
    camera_ids: Optional[List[int]] = None
    email: Optional[EmailStr] = None
    expires_hours: int = 168  # 7 days default
    # 接受後賦予的角色（僅 symotus_admin 發的邀請可指定 reseller，其餘一律 end_user）
    intended_role: str = "end_user"
    # 發 reseller 邀請時可預綁 Camera Backend 帳號，接受者一登入即可管理相機
    camera_email: Optional[str] = None
    camera_user_id: Optional[int] = None

class InviteResponse(BaseModel):
    id: int
    token: str
    invite_url: str
    reseller_id: int
    camera_ids: Optional[List[int]]
    email: Optional[str]
    intended_role: str = "end_user"
    camera_email: Optional[str] = None
    status: str
    expires_at: UtcDatetime
    accepted_by: Optional[int]
    accepted_at: Optional[UtcDatetime]
    created_at: UtcDatetime

    class Config:
        from_attributes = True

class InvitePreview(BaseModel):
    valid: bool
    reason: Optional[str] = None  # expired | revoked | already_used | not_found
    reseller_name: Optional[str] = None
    camera_count: Optional[int] = None
    expires_at: Optional[UtcDatetime] = None


# ── Camera Access ──────────────────────────────────────────
class CameraAccessCreate(BaseModel):
    user_id: int

class CameraAccessResponse(BaseModel):
    id: int
    camera_id: int
    user_id: int
    granted_by: int
    created_at: UtcDatetime
    user: Optional[UserResponse] = None

    class Config:
        from_attributes = True


# ── Tech Support Grant ──────────────────────────────────────────
class TechSupportGrantCreate(BaseModel):
    camera_ids: Optional[List[int]] = None
    duration_hours: int = 48

class TechSupportGrantResponse(BaseModel):
    id: int
    reseller_id: int
    camera_ids: Optional[List[int]]
    expires_at: UtcDatetime
    revoked_at: Optional[UtcDatetime]
    created_at: UtcDatetime

    class Config:
        from_attributes = True


# ── Token Payload ──────────────────────────────────────────
class TokenPayload(BaseModel):
    sub: int  # user_id
    role: str
    reseller_id: Optional[int] = None
    camera_ids: Optional[List[int]] = None  # end_user 可存取的相機 IDs
    tech_support_until: Optional[str] = None  # symotus_admin 獲得的臨時授權到期時間


# ── Billing ──────────────────────────────────────────────────────
class PlanCreate(BaseModel):
    name: str
    description: Optional[str] = None
    monthly_fee: int = 0
    timelapse_quota_secs: int = 0
    storage_quota_gb: int = 0


class PlanUpdate(BaseModel):
    """局部更新：所有欄位皆可選，未帶的欄位不動既有值（PATCH 語意）。"""
    name: Optional[str] = None
    description: Optional[str] = None
    monthly_fee: Optional[int] = None
    timelapse_quota_secs: Optional[int] = None
    storage_quota_gb: Optional[int] = None
    is_active: Optional[bool] = None  # None = 不變更啟用狀態；用來讓已停用的方案能重新啟用


class PlanResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    monthly_fee: int
    timelapse_quota_secs: int
    storage_quota_gb: int
    is_active: bool

    class Config:
        from_attributes = True


class CustomerUpdate(BaseModel):
    """局部更新：未帶的欄位不動既有值（PATCH 語意）。"""
    billing_day: Optional[int] = Field(None, ge=1, le=28)
    note: Optional[str] = None
    payment_method: Optional[Literal["monthly_transfer", "credit_card"]] = None
    statement_day: Optional[int] = Field(None, ge=1, le=28)
    custom_monthly_fee: Optional[int] = Field(None, ge=0)
    commission_type: Optional[Literal["percent", "fixed"]] = None
    # 分潤拆成兩個獨立欄位，各自的單位與上限不再互相牽制：
    # - commission_percent_bps 是萬分比（bps），percent 型別時 10000 = 100%
    # - commission_fixed_amount 是 TWD 整數金額，上限一千萬
    commission_percent_bps: Optional[int] = Field(None, ge=0, le=MAX_COMMISSION_BPS)
    commission_fixed_amount: Optional[int] = Field(None, ge=0, le=MAX_COMMISSION_FIXED)

    @model_validator(mode="after")
    def _commission_type_matches_value(self) -> "CustomerUpdate":
        """commission_type 決定該讀哪一個數值欄位，兩者必須配對，不能單位錯亂。

        規則：
        - type="percent" 時，commission_percent_bps 必須同時有值。
        - type="fixed" 時，commission_fixed_amount 必須同時有值。
        - type=None（清除分潤）時，兩個數值欄位都不該有值，否則會留下對不上
          型別的孤兒數值。
        - 只送數值欄位而不送 commission_type 時，這裡看不到客戶目前已存的
          型別，交給 router 層依既有型別檢查是否對得上。

        用 model_fields_set 判斷「這個請求裡有沒有帶這個 key」，而不是看值是不是
        None——這幾個欄位都允許明確傳 null 來清除設定，若改用 `value is None`
        判斷會把「沒帶」和「帶了 null」混為一談。
        """
        fields = self.model_fields_set
        type_sent = "commission_type" in fields
        if not type_sent:
            return self
        if self.commission_type == "percent":
            if self.commission_percent_bps is None:
                raise ValueError("commission_type 為 percent 時，commission_percent_bps 不可為 null")
            if "commission_fixed_amount" in fields and self.commission_fixed_amount is not None:
                raise ValueError("commission_type 為 percent 時，不可同時設定 commission_fixed_amount")
        elif self.commission_type == "fixed":
            if self.commission_fixed_amount is None:
                raise ValueError("commission_type 為 fixed 時，commission_fixed_amount 不可為 null")
            if "commission_percent_bps" in fields and self.commission_percent_bps is not None:
                raise ValueError("commission_type 為 fixed 時，不可同時設定 commission_percent_bps")
        else:  # commission_type 明確傳 null：清除分潤，兩個數值欄位都不該有值
            if self.commission_percent_bps is not None or self.commission_fixed_amount is not None:
                raise ValueError("commission_type 為 null 時，不可同時設定分潤數值欄位")
        return self


class CustomerResponse(BaseModel):
    user_id: int
    username: str
    email: str
    role: str
    billing_day: int
    frozen: bool
    note: Optional[str] = None
    payment_method: str
    statement_day: int
    custom_monthly_fee: Optional[int] = None
    commission_type: Optional[str] = None
    commission_percent_bps: Optional[int] = None
    commission_fixed_amount: Optional[int] = None
    commission_display: str = ""


class SubscriptionCreate(BaseModel):
    camera_id: int
    customer_id: int
    plan_id: int


class SubscriptionResponse(BaseModel):
    id: int
    camera_id: int
    customer_id: int
    plan_id: int
    plan_name: Optional[str] = None
    monthly_fee: int = 0
    status: str


class InvoiceLineResponse(BaseModel):
    id: int
    camera_id: int
    camera_name: Optional[str] = None
    plan_name: Optional[str] = None
    amount: int


class InvoiceResponse(BaseModel):
    id: int
    customer_id: int
    customer_name: Optional[str] = None
    period: str
    total: int
    status: str
    # 必須用 UtcDatetime（schemas.py:19 既有慣例），不可用裸 datetime：
    # DB 存 naive UTC，裸 datetime 序列化後不帶時區 offset，前端 new Date() 會當成
    # 本地時間，顯示慢 8 小時。
    issued_at: Optional[UtcDatetime] = None
    paid_at: Optional[UtcDatetime] = None


class InvoiceDetailResponse(InvoiceResponse):
    lines: list[InvoiceLineResponse] = []


class CommissionRowResponse(BaseModel):
    customer_id: int
    customer_name: Optional[str] = None
    period: str
    invoice_total: int          # 該期別非作廢發票的總額（分潤基數）
    commission_type: str
    commission_percent_bps: Optional[int] = None
    commission_fixed_amount: Optional[int] = None
    commission_amount: int      # 實際要付給經銷商的金額
    commission_display: str = ""


class MySubscriptionResponse(BaseModel):
    id: int
    camera_id: int
    plan_name: Optional[str] = None
    monthly_fee: int
    status: str


class MyQuotaResponse(BaseModel):
    camera_id: int
    period: str
    timelapse_used_secs: int
    timelapse_total_secs: int
    storage_used_gb: float
    storage_total_gb: int
    state: str  # ok | warned | suspended
