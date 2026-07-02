from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from database import get_db
from models import User, CameraAccess, TechSupportGrant
from schemas import TokenPayload
from config import settings
import secrets

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer_scheme = HTTPBearer()


def to_backend_role(role: Optional[str]) -> str:
    """把 Auth Service 角色字串對映成 Camera Backend 認得的角色。

    Camera Backend 的角色枚舉只有 admin|manager|user（見 docs/core_API.json
    schema UserUpdate）。我方的 `symotus_admin` 它不認得，會被當成普通 user →
    admin-fallback token 其實拿不到 admin 權限（無法存取/刪除他人或孤兒相機，
    回 403）。這裡把 symotus_admin → admin。reseller/end_user 維持原字串：
    後端對非 admin 是依帳號 ownership（email→user）授權，與 role 字串無關。
    """
    return "admin" if role == "symotus_admin" else (role or "user")


def hash_password(password: str) -> str:
    # bcrypt 限制 72 bytes
    return pwd_context.hash(password[:72])

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

def create_access_token(user: User, db: Session) -> str:
    """建立 JWT，end_user 把 camera_ids 寫進 token"""
    camera_ids = None
    tech_support_until = None

    if user.role == "end_user":
        accesses = db.query(CameraAccess).filter(CameraAccess.user_id == user.id).all()
        camera_ids = [a.camera_id for a in accesses]

    if user.role == "symotus_admin":
        # 查有沒有有效的 tech support grant（針對此 admin）
        # 此處簡化：admin token 不帶 camera_ids，由各 endpoint 查 DB
        pass

    payload = {
        "sub": str(user.id),
        "role": user.role,
        "reseller_id": user.reseller_id,
        "camera_ids": camera_ids,
        "tech_support_until": tech_support_until,
        "exp": datetime.utcnow() + timedelta(minutes=settings.JWT_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)

def create_refresh_token() -> str:
    return secrets.token_urlsafe(64)

def decode_token(token: str) -> TokenPayload:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        return TokenPayload(
            sub=int(payload["sub"]),
            role=payload["role"],
            reseller_id=payload.get("reseller_id"),
            camera_ids=payload.get("camera_ids"),
            tech_support_until=payload.get("tech_support_until"),
        )
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")


def create_line_bind_token(user_id: int) -> str:
    """短效簽章 ticket：讓已登入用戶把 LINE 帳號綁到「當前這個 user」。
    夾帶在 LINE OAuth 的 state 裡經 callback 帶回，10 分鐘有效。"""
    payload = {
        "sub": str(user_id),
        "purpose": "line_bind",
        "exp": datetime.utcnow() + timedelta(minutes=10),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_line_bind_token(token: str) -> Optional[int]:
    """驗證綁定 ticket，回傳 user_id；無效/過期/用途不符回 None。"""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        if payload.get("purpose") != "line_bind":
            return None
        return int(payload["sub"])
    except Exception:
        return None


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    payload = decode_token(credentials.credentials)
    user = db.query(User).filter(User.id == payload.sub, User.is_active == True).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user

def require_role(*roles: str):
    def checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in roles:
            raise HTTPException(status_code=403, detail=f"Required role: {roles}")
        return current_user
    return checker

def verify_camera_access(camera_id: int, user: User, db: Session) -> bool:
    """驗證 user 是否有權存取某台相機"""
    if user.role == "symotus_admin":
        # 檢查有沒有有效的 tech support grant
        grant = db.query(TechSupportGrant).filter(
            TechSupportGrant.expires_at > datetime.utcnow(),
            TechSupportGrant.revoked_at == None,
        ).first()
        return grant is not None

    if user.role == "reseller":
        # reseller 可存取自己擁有的相機（向現有後端查詢）
        return True  # 讓現有後端的 owner_id 驗證

    if user.role == "end_user":
        access = db.query(CameraAccess).filter(
            CameraAccess.camera_id == camera_id,
            CameraAccess.user_id == user.id,
        ).first()
        return access is not None

    return False
