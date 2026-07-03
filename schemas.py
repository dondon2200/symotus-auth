from pydantic import BaseModel, EmailStr, PlainSerializer
from typing import Optional, List
from typing_extensions import Annotated
from datetime import datetime, timezone


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
