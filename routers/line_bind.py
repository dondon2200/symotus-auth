"""LINE Login 一鍵綁定。

只做「綁定」，不做登入——平台登入僅有帳密（2026-08-28 起）。
流程：前端取綁定 session → 使用者開 /auth/line/bind-start → 302 至 LINE 授權
→ LINE 回 /auth/line/callback → 寫入 user_line_accounts。
既有的官方帳號綁定碼流程（routers/auth.py + line_webhook）完整保留為備援。
"""
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from database import get_db
from models import User, LineBindSession
from auth import get_current_user
from config import settings
from audit import log_action
from routers.auth import _rate_limit

router = APIRouter(prefix="/auth", tags=["line-bind"])

BIND_SESSION_TTL_MINUTES = 5


def _login_channel_ready() -> bool:
    """Login channel 三個變數齊備才算可用；缺任一項就讓前端退回綁定碼流程。"""
    return bool(settings.LINE_CHANNEL_ID
                and settings.LINE_CLIENT_SECRET
                and settings.LINE_REDIRECT_URI)


@router.post("/me/line/bind-session")
def create_bind_session(request: Request, db: Session = Depends(get_db),
                        current_user: User = Depends(get_current_user)):
    """建立一次性綁定 session，回傳可直接開啟或轉成 QR 的綁定連結。"""
    if not _login_channel_ready():
        raise HTTPException(501, "尚未啟用 LINE 一鍵綁定")
    _rate_limit(request, "line_bind_session", 10)
    sid = secrets.token_urlsafe(32)
    row = LineBindSession(
        sid=sid, user_id=current_user.id,
        expires_at=datetime.utcnow() + timedelta(minutes=BIND_SESSION_TTL_MINUTES))
    db.add(row)
    # 比照 create_line_bind_code：這個端點發出的是「能把 LINE 綁進本帳號」的憑證，要留稽核軌跡
    log_action(db, current_user, "self_line_bind_session", "user", current_user.id,
               "line_bind_sessions")
    db.commit()
    return {
        "sid": sid,
        "bind_url": f"{settings.PUBLIC_BASE_URL}/auth/line/bind-start?s={sid}",
        "expires_at": row.expires_at.isoformat() + "Z",
    }
