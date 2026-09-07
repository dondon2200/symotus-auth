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
from fastapi.responses import HTMLResponse, RedirectResponse
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


LINE_AUTHORIZE_URL = "https://access.line.me/oauth2/v2.1/authorize"


def _page(title: str, body: str, extra_html: str = "") -> HTMLResponse:
    """綁定流程的結果頁。使用者是在 LINE 內建瀏覽器看這一頁，越簡單越好。"""
    return HTMLResponse(f"""<!doctype html><html lang="zh-Hant"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title></head>
<body style="margin:0;font-family:system-ui,-apple-system,'Noto Sans TC',sans-serif;
background:#131313;color:#e5e2e1;display:flex;min-height:100vh;align-items:center;
justify-content:center;padding:24px;">
<div style="max-width:420px;text-align:center;">
<h1 style="font-size:20px;margin:0 0 12px;">{title}</h1>
<p style="font-size:14px;line-height:1.7;color:#a78b7d;margin:0;">{body}</p>
{extra_html}</div></body></html>""")


def _load_session(db: Session, sid: str) -> LineBindSession | None:
    """只回傳未使用且未過期的 session。"""
    if not sid:
        return None
    return (db.query(LineBindSession)
              .filter(LineBindSession.sid == sid,
                      LineBindSession.used_at == None,   # noqa: E711
                      LineBindSession.expires_at > datetime.utcnow())
              .first())


@router.get("/line/bind-start")
def line_bind_start(s: str = "", db: Session = Depends(get_db)):
    """使用者（或掃 QR 的手機）開啟的入口：驗證 session 後轉去 LINE 授權。
    刻意不需要 JWT——手機掃桌機的 QR 時沒有登入態，身分是靠 sid 帶的。"""
    if not _login_channel_ready():
        return _page("尚未啟用", "系統尚未啟用 LINE 一鍵綁定，請改用個人設定頁的綁定碼流程。")
    row = _load_session(db, s)
    if row is None:
        return _page("連結已失效", "這個綁定連結已過期或已使用過，請回到網頁「個人設定」重新產生。")
    params = {
        "response_type": "code",
        "client_id": settings.LINE_CHANNEL_ID,
        "redirect_uri": settings.LINE_REDIRECT_URI,
        "state": row.sid,
        "scope": "profile openid",
        "bot_prompt": "aggressive",
    }
    return RedirectResponse(f"{LINE_AUTHORIZE_URL}?{urlencode(params)}", status_code=302)
