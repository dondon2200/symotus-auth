from pydantic import BaseModel, EmailStr
from typing import Optional, List
from datetime import datetime


# ── Auth ──────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds

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
    created_at: datetime

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

class InviteResponse(BaseModel):
    id: int
    token: str
    invite_url: str
    reseller_id: int
    camera_ids: Optional[List[int]]
    email: Optional[str]
    status: str
    expires_at: datetime
    accepted_by: Optional[int]
    accepted_at: Optional[datetime]
    created_at: datetime

    class Config:
        from_attributes = True

class InvitePreview(BaseModel):
    valid: bool
    reason: Optional[str] = None  # expired | revoked | already_used | not_found
    reseller_name: Optional[str] = None
    camera_count: Optional[int] = None
    expires_at: Optional[datetime] = None


# ── Camera Access ──────────────────────────────────────────
class CameraAccessCreate(BaseModel):
    user_id: int

class CameraAccessResponse(BaseModel):
    id: int
    camera_id: int
    user_id: int
    granted_by: int
    created_at: datetime
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
    expires_at: datetime
    revoked_at: Optional[datetime]
    created_at: datetime

    class Config:
        from_attributes = True


# ── Token Payload ──────────────────────────────────────────
class TokenPayload(BaseModel):
    sub: int  # user_id
    role: str
    reseller_id: Optional[int] = None
    camera_ids: Optional[List[int]] = None  # end_user 可存取的相機 IDs
    tech_support_until: Optional[str] = None  # symotus_admin 獲得的臨時授權到期時間
