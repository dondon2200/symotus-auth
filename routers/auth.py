from fastapi import APIRouter, Depends, HTTPException, Header, Request, Response
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
import httpx, secrets, time

from database import get_db
from models import User, RefreshToken, InviteToken, CameraAccess, UserLineAccount
from schemas import LoginRequest, TokenResponse, RefreshRequest, OAuthCallbackRequest
from auth import (hash_password, verify_password, create_access_token,
                  create_refresh_token, decode_token, get_current_user,
                  to_backend_role, create_line_bind_token, decode_line_bind_token)
from config import settings
from audit import log_action


CAMERA_BACKEND_URL = "https://user.symotus.com"
import os
CAMERA_SERVICE_KEY = os.environ.get("CAMERA_SERVICE_KEY", "")

# ── Host gate ──────────────────────────────────────────────────────────────
ADMIN_HOSTS = {"admin.symotus.com"}

def _get_host(request: Request) -> str:
    return (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or ""
    ).split(":")[0].lower()

def _check_admin_host(host: str, role: str):
    """admin.symotus.com 入口僅限 symotus_admin；否則 403。"""
    if host in ADMIN_HOSTS and role != "symotus_admin":
        raise HTTPException(status_code=403, detail="此入口僅限管理員登入")
# ───────────────────────────────────────────────────────────────────────────

async def get_camera_token(user_id: int, email: str, role: str, camera_email: Optional[str] = None) -> dict:
    """向 Camera Backend 換取 camera token"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            actual_email = camera_email or email
            resp = await client.post(
                f"{CAMERA_BACKEND_URL}/internal/auth/token",
                headers={"x-service-key": CAMERA_SERVICE_KEY},
                json={"user_id": user_id, "email": actual_email, "role": to_backend_role(role)},
            )
            print(f"[Camera token] {actual_email} -> {resp.status_code}: {resp.text[:100]}")
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        print(f"[Camera token] Failed: {e}")
    return {}

router = APIRouter(prefix="/auth", tags=["auth"])


# F-7：OAuth 登入完成後不把 token 放 URL，改以一次性短效 code 交換（單一 auth 容器，記憶體即可）
_login_handoff: dict = {}   # code -> (bundle: dict, expires_at: float)
_HANDOFF_TTL = 120          # 秒

def _store_login_handoff(bundle: dict) -> str:
    now = time.time()
    for k in [k for k, (_, exp) in list(_login_handoff.items()) if exp < now]:
        _login_handoff.pop(k, None)
    code = secrets.token_urlsafe(24)
    _login_handoff[code] = (bundle, now + _HANDOFF_TTL)
    return code


# F-8：簡易記憶體限流（單一 auth 容器），擋登入/註冊/換碼爆破
_rl_buckets: dict = {}   # "bucket:ip" -> [count, reset_at]

def _rate_limit(request: Request, bucket: str, max_req: int, window: int = 60):
    xff = request.headers.get("x-forwarded-for") or ""
    ip = xff.split(",")[0].strip() or (request.client.host if request.client else "unknown")
    key = f"{bucket}:{ip}"
    now = time.time()
    entry = _rl_buckets.get(key)
    if not entry or now > entry[1]:
        _rl_buckets[key] = [1, now + window]
        return
    if entry[0] >= max_req:
        raise HTTPException(429, "請求過於頻繁，請稍後再試")
    entry[0] += 1


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    _rate_limit(request, "login", 10)
    user = db.query(User).filter(
        (User.username == body.username) | (User.email == body.username)
    ).first()
    if not user or not user.hashed_password or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="帳號或密碼錯誤")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="帳號已停用")

    # admin.symotus.com 入口僅限平台管理員
    _check_admin_host(_get_host(request), user.role)

    access_token = create_access_token(user, db)
    refresh = create_refresh_token()
    db.add(RefreshToken(user_id=user.id, token=refresh,
        expires_at=datetime.utcnow() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)))
    db.commit()
    camera_tokens = await get_camera_token(user.id, user.email, user.role, user.camera_email)
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
    camera_tokens = await get_camera_token(user.id, user.email, user.role, user.camera_email)
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

def _me_payload(user: User) -> dict:
    """/auth/me 系列端點共用的使用者序列化。"""
    line_accounts = [
        {"id": a.id, "display_name": a.display_name,
         "created_at": a.created_at.isoformat() if a.created_at else None}
        for a in user.line_accounts
    ]
    return {"id": user.id, "username": user.username,
            "email": user.email, "full_name": user.full_name,
            "role": user.role, "reseller_id": user.reseller_id,
            "is_active": user.is_active,
            "has_password": user.hashed_password is not None,
            "line_accounts": line_accounts,
            "line_linked": len(line_accounts) > 0,
            "created_at": user.created_at.isoformat() if user.created_at else None}


@router.get("/me")
def me(current_user: User = Depends(get_current_user)):
    return _me_payload(current_user)


class UpdateMeRequest(BaseModel):
    full_name: Optional[str] = None


@router.put("/me")
def update_me(body: UpdateMeRequest, db: Session = Depends(get_db),
              current_user: User = Depends(get_current_user)):
    """self-service 更新。刻意只收 full_name：username/email/role 不開放自助修改，
    且本端點不接受 user_id，結構上就不可能改到別人。

    PATCH 語意：body 完全未帶 full_name 欄位時視為「不修改」，而非清空——
    用 model_fields_set 區分「欄位不存在」與「明確傳 null」。"""
    if "full_name" in body.model_fields_set:
        current_user.full_name = (body.full_name or "").strip() or None
        log_action(db, current_user, "self_update_profile", "user", current_user.id, "full_name")
        db.commit()
        db.refresh(current_user)
    return _me_payload(current_user)


MIN_PASSWORD_LENGTH = 8


class ChangePasswordRequest(BaseModel):
    current_password: Optional[str] = None
    new_password: str
    keep_refresh_token: Optional[str] = None


@router.post("/me/password")
def change_my_password(body: ChangePasswordRequest, db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    """設定或變更自己的密碼。

    OAuth-only 帳號（hashed_password 為 None）免帶舊密碼——持有有效 access token
    即已完成身分驗證，要求一個不存在的舊密碼會讓這些帳號永遠設不了密碼。
    """
    if len(body.new_password or "") < MIN_PASSWORD_LENGTH:
        raise HTTPException(400, f"新密碼至少需要 {MIN_PASSWORD_LENGTH} 個字元")

    if current_user.hashed_password is not None:
        if not body.current_password or not verify_password(
                body.current_password, current_user.hashed_password):
            raise HTTPException(401, "目前密碼不正確")

    current_user.hashed_password = hash_password(body.new_password)

    # 密碼換了，其他裝置上的 session 一律作廢；呼叫端自己那一枚由 keep_refresh_token 指定保留
    q = db.query(RefreshToken).filter(RefreshToken.user_id == current_user.id,
                                      RefreshToken.revoked == False)
    if body.keep_refresh_token:
        q = q.filter(RefreshToken.token != body.keep_refresh_token)
    for token in q.all():
        token.revoked = True

    log_action(db, current_user, "self_change_password", "user", current_user.id, "hashed_password")
    db.commit()
    return {"message": "密碼已更新"}


@router.post("/me/logout-all")
def logout_all_devices(db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    """撤銷本帳號所有 refresh token（含呼叫端自己），前端隨後導回登入頁。"""
    count = 0
    for token in db.query(RefreshToken).filter(
            RefreshToken.user_id == current_user.id,
            RefreshToken.revoked == False).all():
        token.revoked = True
        count += 1
    log_action(db, current_user, "self_logout_all", "user", current_user.id,
               f"revoked={count}")
    db.commit()
    return {"message": "已登出所有裝置"}


@router.post("/me/unlink/{provider}")
def unlink_provider(provider: str, current_user: User = Depends(get_current_user)):
    """已停用：google_id/line_id 舊式綁定已無登入用途；LINE 逐筆解綁見
    `DELETE /auth/me/line/{id}`（Task 2）。"""
    raise HTTPException(410, "第三方登入已停用")


@router.delete("/me/line/{line_account_id}")
def unlink_line_account(line_account_id: int, db: Session = Depends(get_db),
                        current_user: User = Depends(get_current_user)):
    """解除單一 LINE 綁定（user_line_accounts 逐筆解綁，一帳號可綁多個 LINE）。"""
    row = db.query(UserLineAccount).filter(
        UserLineAccount.id == line_account_id,
        UserLineAccount.user_id == current_user.id,
    ).first()
    if not row:
        raise HTTPException(404, "找不到該 LINE 綁定")
    db.delete(row)
    log_action(db, current_user, "self_unlink_line", "user", current_user.id, "user_line_accounts")
    db.commit()
    db.refresh(current_user)
    return _me_payload(current_user)


class ExchangeRequest(BaseModel):
    code: str

@router.post("/exchange", response_model=TokenResponse)
def exchange_login_code(body: ExchangeRequest, request: Request):
    """F-7：用一次性 code 換取登入 token（OAuth callback 不再把 token 放 URL）"""
    _rate_limit(request, "exchange", 30)
    entry = _login_handoff.pop(body.code, None)  # 一次性使用
    if not entry:
        raise HTTPException(400, "登入碼無效或已使用")
    bundle, exp = entry
    if exp < time.time():
        raise HTTPException(400, "登入碼已過期")
    return TokenResponse(**bundle)


@router.get("/google/url")
def google_url(invite_token: str = None):
    raise HTTPException(410, "第三方登入已停用")

@router.post("/google/token", response_model=TokenResponse)
async def google_token(body: OAuthCallbackRequest, request: Request, db: Session = Depends(get_db)):
    raise HTTPException(410, "第三方登入已停用")


@router.get("/google/bind-url")
def google_bind_url(current_user: User = Depends(get_current_user)):
    """已停用：Google OAuth 僅保留 LINE 綁定用途，Google 綁定/登入一律關閉。"""
    raise HTTPException(410, "第三方登入已停用")


@router.post("/me/link/google")
async def link_google(body: OAuthCallbackRequest, db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    raise HTTPException(410, "第三方登入已停用")


@router.get("/line/url")
def line_url(response: Response, invite_token: str = None):
    """已停用：LINE 登入關閉，OAuth 只保留 /line/bind-url 綁定流程。"""
    raise HTTPException(410, "第三方登入已停用")

@router.get("/line/bind-url")
def line_bind_url(response: Response, next: str = "/notifications",
                  current_user: User = Depends(get_current_user)):
    """A：已登入用戶取得「綁定 LINE」授權連結。state 夾帶簽章 ticket 標識當前用戶，
    /line/callback 依 ticket 把 LINE 綁到「這個帳號」，而非以 email 比對或另開新帳號。"""
    ticket = create_line_bind_token(current_user.id, next)
    state = f"{secrets.token_urlsafe(16)}:bind:{ticket}"
    response.set_cookie("line_oauth_state", state, max_age=600,
                        httponly=True, secure=True, samesite="lax", path="/")
    params = f"response_type=code&client_id={settings.LINE_CLIENT_ID}&redirect_uri={settings.LINE_REDIRECT_URI}&state={state}&scope=profile openid email"
    return {"auth_url": f"https://access.line.me/oauth2/v2.1/authorize?{params}", "state": state}

@router.post("/line/token", response_model=TokenResponse)
async def line_token(body: OAuthCallbackRequest, request: Request, db: Session = Depends(get_db)):
    """已停用：LINE 登入關閉，OAuth 只保留 /line/bind-url 綁定流程。"""
    raise HTTPException(410, "第三方登入已停用")


class UserCreateInternal(BaseModel):
    username: str
    email: str
    full_name: Optional[str] = None
    password: str
    role: str = "end_user"          # 預設最低權限；公開註冊一律強制 end_user
    camera_email: Optional[str] = None
    invite_token: Optional[str] = None

@router.post("/register")
async def register(body: UserCreateInternal, request: Request, db: Session = Depends(get_db),
                   x_service_key: str = Header(None),
                   authorization: str = Header(None)):
    """建立帳號。
    F-1 修補：公開註冊一律需「有效邀請連結」，且角色強制 end_user（杜絕外部指定 role 提權）。
    內部建帳號（指定 role / camera_email）需帶正確 x-service-key 或 symotus_admin JWT。
    """
    is_internal = bool(CAMERA_SERVICE_KEY) and x_service_key == CAMERA_SERVICE_KEY
    if not is_internal and authorization:
        try:
            token = authorization.replace("Bearer ", "")
            payload = decode_token(token)
            if payload.get("role") == "symotus_admin":
                is_internal = True
        except Exception:
            pass
    if not is_internal:
        _rate_limit(request, "register", 5)

    invite = None
    if not is_internal:
        # 公開路徑：必須有有效邀請；角色固定 end_user
        if not body.invite_token:
            raise HTTPException(403, "需要有效的邀請連結才能註冊")
        invite = db.query(InviteToken).filter(
            InviteToken.token == body.invite_token,
            InviteToken.status == "pending",
            InviteToken.expires_at > datetime.utcnow(),
        ).first()
        if not invite:
            raise HTTPException(400, "邀請連結無效或已過期")
        if invite.email and invite.email != body.email:
            raise HTTPException(400, "此邀請連結限定特定 Email 使用")

    # 公開邀請路徑：角色依邀請指定（「reseller」需發邀請者為 platform admin）
    invite_role = "end_user"
    if invite and invite.intended_role == "reseller":
        inviter = db.query(User).filter(User.id == invite.reseller_id).first()
        if inviter and inviter.role == "symotus_admin":
            invite_role = "reseller"

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
        role=body.role if is_internal else invite_role,
        is_active=True,
        reseller_id=invite.reseller_id if invite else None,
        camera_email=(body.camera_email if is_internal else (invite.camera_email if invite else None)),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # 邀請帶相機 → 建立 camera_access（與 OAuth _oauth_finish 行為一致；同時修掉 email 邀請註冊不掛相機的舊 bug）
    if invite:
        if invite.camera_user_id:
            user.camera_user_id = invite.camera_user_id
        if invite.camera_ids:
            for cam_id in invite.camera_ids:
                db.add(CameraAccess(camera_id=cam_id, user_id=user.id, granted_by=invite.reseller_id,
                                    invitation_id=0))  # 非邀請連結來源（哨兵，避免撤銷連結時 NULL fallback 誤刪）
        invite.status = "accepted"; invite.accepted_by = user.id; invite.accepted_at = datetime.utcnow()
        db.commit()
        db.refresh(user)

    camera_tokens = await get_camera_token(user.id, user.email, user.role, user.camera_email)
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


@router.get("/line/callback")
async def line_callback(code: str, state: str = "", request: Request = None, db: Session = Depends(get_db)):
    from fastapi.responses import RedirectResponse
    frontend = settings.FRONTEND_URL
    # round-trip 驗證：LINE 回傳的 state 必須等於 /line/url 時寫入 cookie 的值
    saved_state = request.cookies.get("line_oauth_state") if request else None
    if not state or not saved_state or not secrets.compare_digest(state, saved_state):
        resp = RedirectResponse(f"{frontend}/login?error=state_mismatch")
        resp.delete_cookie("line_oauth_state", path="/")
        return resp
    try:
        bind_user_id = None
        bind_next = "/notifications"
        # A：state 夾帶 bind ticket → 綁定流程（把 LINE 綁到當前登入用戶，而非登入/建帳）
        if state and ":bind:" in state:
            decoded = decode_line_bind_token(state.split(":bind:", 1)[1])
            if decoded:
                bind_user_id, bind_next = decoded

        async with httpx.AsyncClient() as client:
            r = await client.post("https://api.line.me/oauth2/v2.1/token", data={
                "grant_type": "authorization_code", "code": code,
                "redirect_uri": settings.LINE_REDIRECT_URI,
                "client_id": settings.LINE_CLIENT_ID,
                "client_secret": settings.LINE_CLIENT_SECRET,
            })
            td = r.json()
            if "access_token" not in td:
                print(f"[LINE callback] token error: {td}")
                return RedirectResponse(f"{frontend}/login?error=line_failed")
            r2 = await client.get("https://api.line.me/v2/profile",
                                   headers={"Authorization": f"Bearer {td['access_token']}"})
            profile = r2.json()

        # A：綁定流程 —— 把 LINE userId 綁到 ticket 指定的既有用戶
        if bind_user_id is not None:
            line_uid = profile["userId"]
            target = db.query(User).filter(User.id == bind_user_id).first()
            existing = db.query(UserLineAccount).filter_by(line_user_id=line_uid).first()
            if not target:
                resp = RedirectResponse(f"{frontend}{bind_next}?line_bind=error")
            elif existing and existing.user_id != target.id:
                resp = RedirectResponse(f"{frontend}{bind_next}?line_bind=conflict")
            else:
                if not existing:
                    db.add(UserLineAccount(
                        user_id=target.id, line_user_id=line_uid,
                        display_name=profile.get("displayName"),
                        picture_url=profile.get("pictureUrl")))
                log_action(db, target, "self_link_line", "user", target.id, "line_id")
                db.commit()
                resp = RedirectResponse(f"{frontend}{bind_next}?line_bind=ok")
            resp.delete_cookie("line_oauth_state", path="/")
            return resp

        # Task 3：LINE 登入已停用，非 bind 分支一律導回登入頁，不建帳、不發 token
        resp = RedirectResponse(f"{frontend}/login?error=oauth_disabled")
        resp.delete_cookie("line_oauth_state", path="/")
        return resp
    except HTTPException as he:
        if he.status_code == 403 and "管理員" in he.detail:
            return RedirectResponse(f"{frontend}/login?error=admin_only")
        return RedirectResponse(f"{frontend}/login?error=line_failed")
    except Exception as e:
        print(f"[LINE callback] unexpected error: {e}")
        return RedirectResponse(f"{frontend}/login?error=line_failed")
