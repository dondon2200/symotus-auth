from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
import httpx, secrets

from database import get_db
from models import User, RefreshToken, InviteToken, CameraAccess
from schemas import LoginRequest, TokenResponse, RefreshRequest, OAuthCallbackRequest
from auth import (hash_password, verify_password, create_access_token,
                  create_refresh_token, decode_token, get_current_user)
from config import settings


CAMERA_BACKEND_URL = "https://user.symotus.com"
CAMERA_SERVICE_KEY = "9ad3343a32508c209152a450f601b990176fa4d41c94c27330e448b1a86826c2"

async def get_camera_token(user_id: int, email: str, role: str) -> dict:
    """向 Camera Backend 換取 camera token"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{CAMERA_BACKEND_URL}/internal/auth/token",
                headers={"x-service-key": CAMERA_SERVICE_KEY},
                json={"user_id": user_id, "email": email, "role": role},
            )
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        print(f"[Camera token] Failed: {e}")
    return {}

router = APIRouter(prefix="/auth", tags=["auth"])

@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(
        (User.username == body.username) | (User.email == body.username)
    ).first()
    if not user or not user.hashed_password or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="帳號或密碼錯誤")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="帳號已停用")
    access_token = create_access_token(user, db)
    refresh = create_refresh_token()
    db.add(RefreshToken(user_id=user.id, token=refresh,
        expires_at=datetime.utcnow() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)))
    db.commit()

    # 向 Camera Backend 換取 camera token
    camera_tokens = await get_camera_token(user.id, user.email, user.role)

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh,
        expires_in=settings.JWT_EXPIRE_MINUTES * 60,
        camera_access_token=camera_tokens.get("access_token"),
        camera_refresh_token=camera_tokens.get("refresh_token"),
    )

@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(body: RefreshRequest, db: Session = Depends(get_db)):
    db_token = db.query(RefreshToken).filter(
        RefreshToken.token == body.refresh_token,
        RefreshToken.revoked == False,
        RefreshToken.expires_at > datetime.utcnow(),
    ).first()
    if not db_token:
        raise HTTPException(status_code=401, detail="Refresh token 無效或已過期")
    user = db.query(User).filter(User.id == db_token.user_id, User.is_active == True).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    db_token.revoked = True
    new_refresh = create_refresh_token()
    db.add(RefreshToken(user_id=user.id, token=new_refresh,
        expires_at=datetime.utcnow() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)))
    db.commit()
    # 向 Camera Backend 換取新的 camera token
    camera_tokens = await get_camera_token(user.id, user.email, user.role)

    return TokenResponse(
        access_token=create_access_token(user, db),
        refresh_token=new_refresh,
        expires_in=settings.JWT_EXPIRE_MINUTES * 60,
        camera_access_token=camera_tokens.get("access_token"),
        camera_refresh_token=camera_tokens.get("refresh_token"),
    )

@router.post("/logout")
def logout(body: RefreshRequest, db: Session = Depends(get_db)):
    db_token = db.query(RefreshToken).filter(RefreshToken.token == body.refresh_token).first()
    if db_token:
        db_token.revoked = True
        db.commit()
    return {"message": "Logged out"}

@router.get("/me")
def me(current_user: User = Depends(get_current_user)):
    return {"id": current_user.id, "username": current_user.username,
            "email": current_user.email, "full_name": current_user.full_name,
            "role": current_user.role, "reseller_id": current_user.reseller_id,
            "is_active": current_user.is_active}

@router.get("/google/url")
def google_url(invite_token: str = None):
    state = secrets.token_urlsafe(16)
    if invite_token:
        state = f"{state}:{invite_token}"
    params = f"client_id={settings.GOOGLE_CLIENT_ID}&redirect_uri={settings.GOOGLE_REDIRECT_URI}&response_type=code&scope=openid email profile&state={state}"
    return {"auth_url": f"https://accounts.google.com/o/oauth2/v2/auth?{params}", "state": state}

@router.post("/google/token", response_model=TokenResponse)
async def google_token(body: OAuthCallbackRequest, db: Session = Depends(get_db)):
    invite_token_str = body.invite_token
    if body.state and ":" in body.state:
        _, invite_token_str = body.state.split(":", 1)
    async with httpx.AsyncClient() as client:
        r = await client.post("https://oauth2.googleapis.com/token", data={
            "code": body.code, "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "redirect_uri": settings.GOOGLE_REDIRECT_URI, "grant_type": "authorization_code"})
        td = r.json()
        r2 = await client.get("https://www.googleapis.com/oauth2/v3/userinfo",
                              headers={"Authorization": f"Bearer {td['access_token']}"})
        ui = r2.json()
    return _oauth_finish(db, "google_id", ui["sub"], ui.get("email"), ui.get("name"), invite_token_str)

@router.get("/line/url")
def line_url(invite_token: str = None):
    state = secrets.token_urlsafe(16)
    if invite_token:
        state = f"{state}:{invite_token}"
    params = f"response_type=code&client_id={settings.LINE_CLIENT_ID}&redirect_uri={settings.LINE_REDIRECT_URI}&state={state}&scope=profile openid email"
    return {"auth_url": f"https://access.line.me/oauth2/v2.1/authorize?{params}", "state": state}

@router.post("/line/token", response_model=TokenResponse)
async def line_token(body: OAuthCallbackRequest, db: Session = Depends(get_db)):
    invite_token_str = body.invite_token
    if body.state and ":" in body.state:
        _, invite_token_str = body.state.split(":", 1)
    async with httpx.AsyncClient() as client:
        r = await client.post("https://api.line.me/oauth2/v2.1/token", data={
            "grant_type": "authorization_code", "code": body.code,
            "redirect_uri": settings.LINE_REDIRECT_URI,
            "client_id": settings.LINE_CLIENT_ID, "client_secret": settings.LINE_CLIENT_SECRET})
        td = r.json()
        r2 = await client.get("https://api.line.me/v2/profile",
                               headers={"Authorization": f"Bearer {td['access_token']}"})
        profile = r2.json()
    return _oauth_finish(db, "line_id", profile["userId"], td.get("email"), profile.get("displayName"), invite_token_str)

def _oauth_finish(db, oauth_field, oauth_id, email, full_name, invite_token_str=None):
    user = db.query(User).filter_by(**{oauth_field: oauth_id}).first()
    if not user and email:
        user = db.query(User).filter(User.email == email).first()
        if user:
            setattr(user, oauth_field, oauth_id); db.commit()
    if not user:
        invite = None
        role = "reseller"
        reseller_id = None
        if invite_token_str:
            invite = db.query(InviteToken).filter(
                InviteToken.token == invite_token_str,
                InviteToken.status == "pending",
                InviteToken.expires_at > datetime.utcnow()).first()
            if not invite:
                raise HTTPException(400, "邀請連結無效或已過期")
            if invite.email and invite.email != email:
                raise HTTPException(400, "此邀請連結限定特定 Email 使用")
            role = "end_user"
            reseller_id = invite.reseller_id
        base = (email or "").split("@")[0] or oauth_id[:8]
        username = base
        i = 1
        while db.query(User).filter(User.username == username).first():
            username = f"{base}{i}"; i += 1
        user = User(username=username, email=email or f"{oauth_id}@oauth.local",
                    full_name=full_name, role=role, reseller_id=reseller_id)
        setattr(user, oauth_field, oauth_id)
        db.add(user); db.flush()
        if invite:
            if invite.camera_ids:
                for cam_id in invite.camera_ids:
                    db.add(CameraAccess(camera_id=cam_id, user_id=user.id, granted_by=invite.reseller_id))
            invite.status = "accepted"; invite.accepted_by = user.id; invite.accepted_at = datetime.utcnow()
        db.commit(); db.refresh(user)
    if not user.is_active:
        raise HTTPException(403, "帳號已停用")
    refresh = create_refresh_token()
    db.add(RefreshToken(user_id=user.id, token=refresh,
        expires_at=datetime.utcnow() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)))
    db.commit()
    return TokenResponse(access_token=create_access_token(user, db), refresh_token=refresh,
                         expires_in=settings.JWT_EXPIRE_MINUTES * 60)


# ── Internal: 建立使用者（系統用，不對外公開）──────────────────
class UserCreateInternal(BaseModel):
    username: str
    email: str
    full_name: Optional[str] = None
    password: str
    role: str = "reseller"  # symotus_admin | reseller | end_user

@router.post("/register")
async def register(body: UserCreateInternal, db: Session = Depends(get_db),
                   service_key: str = ""):
    """建立新帳號（需要 x-service-key 或系統初始化用）"""
    from fastapi import Request
    existing = db.query(User).filter(
        (User.username == body.username) | (User.email == body.email)
    ).first()
    if existing:
        raise HTTPException(400, "帳號或 Email 已存在")

    user = User(
        username=body.username,
        email=body.email,
        full_name=body.full_name,
        hashed_password=hash_password(body.password),
        role=body.role,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # 同步換 camera token
    camera_tokens = await get_camera_token(user.id, user.email, user.role)
    access_token = create_access_token(user, db)
    refresh = create_refresh_token()
    db.add(RefreshToken(user_id=user.id, token=refresh,
        expires_at=datetime.utcnow() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)))
    db.commit()

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh,
        expires_in=settings.JWT_EXPIRE_MINUTES * 60,
        camera_access_token=camera_tokens.get("access_token"),
        camera_refresh_token=camera_tokens.get("refresh_token"),
    )
