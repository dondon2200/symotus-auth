from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from database import get_db
from models import User, CameraAccess
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


_DEFAULT_BIND_NEXT = "/notifications"


def _safe_next(path: Optional[str]) -> str:
    """只接受站內絕對路徑，擋掉 open redirect。"""
    if not path or not path.startswith("/") or path.startswith("//"):
        return _DEFAULT_BIND_NEXT
    return path


def create_line_bind_token(user_id: int, next_path: str = _DEFAULT_BIND_NEXT) -> str:
    """短效簽章 ticket：讓已登入用戶把 LINE 帳號綁到「當前這個 user」。
    夾帶在 LINE OAuth 的 state 裡經 callback 帶回，10 分鐘有效。
    next_path 是綁定完成後前端要回到的站內路徑。"""
    payload = {
        "sub": str(user_id),
        "purpose": "line_bind",
        "next": _safe_next(next_path),
        "exp": datetime.utcnow() + timedelta(seconds=600),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_line_bind_token(token: str) -> Optional[tuple]:
    """驗證綁定 ticket，回傳 (user_id, next_path)；無效/過期/用途不符回 None。"""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        if payload.get("purpose") != "line_bind":
            return None
        return int(payload["sub"]), _safe_next(payload.get("next"))
    except Exception:
        return None


def create_google_bind_token(user_id: int) -> str:
    """短效簽章 ticket：讓已登入用戶把 Google 帳號綁到「當前這個 user」。
    夾帶在 Google OAuth 的 state 裡經 callback 帶回，10 分鐘有效。"""
    payload = {
        "sub": str(user_id),
        "purpose": "google_bind",
        "exp": datetime.utcnow() + timedelta(minutes=10),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_google_bind_token(token: str) -> Optional[int]:
    """驗證綁定 ticket，回傳 user_id；無效/過期/用途不符回 None。"""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        if payload.get("purpose") != "google_bind":
            return None
        return int(payload["sub"])
    except Exception:
        return None


def create_gdrive_oauth_ticket(user_id: int) -> str:
    """短效簽章 ticket：整頁 redirect 授權時，讓 Google callback 認得是哪個使用者。

    callback 是 Google 直接打過來的 top-level GET，沒有 Authorization header，
    所以身分只能靠 state 裡夾帶的這張 ticket 帶回來。10 分鐘有效。
    """
    payload = {
        "sub": str(user_id),
        "purpose": "gdrive_oauth",
        "jti": secrets.token_urlsafe(16),  # 避免同一秒內連續簽發出現位元組完全相同的 token
        "exp": datetime.utcnow() + timedelta(minutes=10),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_gdrive_oauth_ticket(token: str) -> Optional[int]:
    """驗證 ticket，回傳 user_id；無效／過期／用途不符回 None。"""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        if payload.get("purpose") != "gdrive_oauth":
            return None
        return int(payload["sub"])
    except Exception:
        return None


def create_video_ticket(user_id: int, job_id: int) -> str:
    """短效簽章 ticket：讓瀏覽器能用一般 <a href> 下載影片。

    下載是 top-level 導覽，帶不了 Authorization header（JWT 存在 localStorage
    不是 cookie），所以身分只能夾在 query string。ticket 綁定 user + job，
    30 分鐘有效——足夠點擊，又不至於外流後長期可用。
    """
    payload = {
        "sub": str(user_id),
        "job": int(job_id),
        "purpose": "gdrive_video",
        "jti": secrets.token_urlsafe(16),
        "exp": datetime.utcnow() + timedelta(minutes=30),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_video_ticket(token: str) -> Optional[tuple[int, int]]:
    """驗證影片下載 ticket，回傳 (user_id, job_id)；無效／過期／用途不符回 None。"""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        if payload.get("purpose") != "gdrive_video":
            return None
        return int(payload["sub"]), int(payload["job"])
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

# （已移除死碼 verify_camera_access：從未被呼叫，且其 admin 分支語意與
#   2026-07-27 決策「admin 不受 TechSupportGrant 限制」不符。實際權限判斷
#   在 routers/cameras.py 的 get_allowed_camera_ids 與各 gate。）
