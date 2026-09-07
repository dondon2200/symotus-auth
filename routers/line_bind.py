"""LINE Login 一鍵綁定。

只做「綁定」，不做登入——平台登入僅有帳密（2026-08-28 起）。
流程：前端取綁定 session → 使用者開 /auth/line/bind-start → 302 至 LINE 授權
→ LINE 回 /auth/line/callback → 寫入 user_line_accounts。
既有的官方帳號綁定碼流程（routers/auth.py + line_webhook）完整保留為備援。
"""
import html
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from database import get_db
from models import User, LineBindSession, UserLineAccount
from auth import get_current_user
from config import settings
from audit import log_action
from routers.auth import _rate_limit
from routers.line_webhook import line_push, _clear_history

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
    """綁定流程的結果頁。使用者是在 LINE 內建瀏覽器看這一頁，越簡單越好。

    title/body 一律經 HTML 轉義：後續任務會把帳號名稱與 LINE 顯示名稱（使用者完全可控）
    帶進來，不轉義就是 XSS。extra_html 刻意不轉義，呼叫者必須自行確保安全
    （目前僅用於程式內寫死的加好友按鈕）。
    """
    safe_title = html.escape(title)
    safe_body = html.escape(body)
    return HTMLResponse(f"""<!doctype html><html lang="zh-Hant"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{safe_title}</title></head>
<body style="margin:0;font-family:system-ui,-apple-system,'Noto Sans TC',sans-serif;
background:#131313;color:#e5e2e1;display:flex;min-height:100vh;align-items:center;
justify-content:center;padding:24px;">
<div style="max-width:420px;text-align:center;">
<h1 style="font-size:20px;margin:0 0 12px;">{safe_title}</h1>
<p style="font-size:14px;line-height:1.7;color:#a78b7d;margin:0;">{safe_body}</p>
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


LINE_TOKEN_URL = "https://api.line.me/oauth2/v2.1/token"
LINE_VERIFY_URL = "https://api.line.me/oauth2/v2.1/verify"
LINE_FRIENDSHIP_URL = "https://api.line.me/friendship/v1/status"


# 連線逾時／DNS 失敗／回應非 JSON 都要回 None 讓上層顯示結果頁，不能讓例外變成 500
async def _exchange_code(code: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(LINE_TOKEN_URL, data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.LINE_REDIRECT_URI,
                "client_id": settings.LINE_CHANNEL_ID,
                "client_secret": settings.LINE_CLIENT_SECRET,
            })
            return r.json() if r.is_success else None
    except Exception:
        return None


# 連線逾時／DNS 失敗／回應非 JSON 都要回 None 讓上層顯示結果頁，不能讓例外變成 500
async def _verify_id_token(id_token: str) -> dict | None:
    """web login 的 id_token 是 HS256，不自行驗簽，交給 LINE 的 verify 端點。"""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(LINE_VERIFY_URL, data={
                "id_token": id_token, "client_id": settings.LINE_CHANNEL_ID})
            return r.json() if r.is_success else None
    except Exception:
        return None


# 連線逾時／DNS 失敗／回應非 JSON 都要回 None（符合本函式的三態語意），不能讓例外變成 500
async def _friend_flag(access_token: str) -> bool | None:
    """是否已加官方帳號好友。沒加好友就收不到任何推播，必須主動查而不是靠推播失敗推論。

    三態：True＝是好友、False＝明確不是、None＝查不到。
    None 的主因是 Login channel 沒有連結官方帳號（此 API 需要連結才有意義）。
    這種情況不能當成「不是好友」——否則連早就加過好友的人都會看到警告。
    """
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(LINE_FRIENDSHIP_URL,
                            headers={"Authorization": f"Bearer {access_token}"})
            if not r.is_success:
                return None
            return bool(r.json().get("friendFlag"))
    except Exception:
        return None


async def _safe_push(line_user_id: str, text: str) -> None:
    """推播失敗不得影響已完成的綁定：此時資料已寫入、session 已消耗，
    讓例外冒出去會變成 500 頁，使用者以為失敗又無法重試（連結已作廢）。"""
    try:
        await line_push(line_user_id, [{"type": "text", "text": text}])
    except Exception:
        pass


_ADD_FRIEND_BUTTON = """<a href="https://line.me/R/ti/p/{oa}" style="display:inline-block;
margin-top:16px;background:#06C755;color:#fff;text-decoration:none;border-radius:24px;
padding:10px 24px;font-size:14px;font-weight:700;">加入官方帳號好友</a>"""


@router.get("/line/callback")
async def line_bind_callback(code: str = "", state: str = "", error: str = "",
                             db: Session = Depends(get_db)):
    """LINE 授權導回點。只做綁定，不發登入 token。"""
    if error or not code:
        return _page("綁定未完成", "你在 LINE 授權頁取消了綁定。回到網頁即可重新開始。")
    row = _load_session(db, state)
    if row is None:
        return _page("連結已失效", "這個綁定連結已過期或已使用過，請回到網頁「個人設定」重新產生。")

    tokens = await _exchange_code(code)
    if not tokens or not tokens.get("id_token"):
        return _page("綁定失敗", "與 LINE 交換憑證時失敗，請稍後再試一次。")
    claims = await _verify_id_token(tokens["id_token"])
    line_user_id = (claims or {}).get("sub")
    if not line_user_id:
        return _page("綁定失敗", "無法驗證 LINE 身分，請稍後再試一次。")

    row.used_at = datetime.utcnow()
    user = row.user
    others = [a.line_user_id for a in user.line_accounts
              if a.line_user_id != line_user_id]

    # 同一支 LINE 的其他綁定退為非作用中（AI 助理的「作用中帳號」語意，通知不看此欄位）。
    # 必須在判斷 existing 之前做：否則冪等路徑只把本列設 True，舊帳號那列仍是 True，
    # line_webhook._resolve_user 取 id 最小的作用中列 → 使用者剛綁定 B 卻仍以 A 身分操作。
    # 順序比照 routers/line_webhook.py 綁定碼流程。
    db.query(UserLineAccount).filter(
        UserLineAccount.line_user_id == line_user_id).update({"is_active": False})

    existing = db.query(UserLineAccount).filter_by(
        user_id=user.id, line_user_id=line_user_id).first()
    if existing:
        existing.is_active = True
        db.commit()
        _clear_history(line_user_id)   # 作用中帳號變了，AI 對話歷史不能沿用舊帳號的
        return _page("已經綁定過了",
                     f"這支 LINE 已經綁在帳號 {user.username}，不需要重複綁定。可以關閉此頁。")

    db.add(UserLineAccount(
        user_id=user.id, line_user_id=line_user_id,
        display_name=(claims or {}).get("name"),
        picture_url=(claims or {}).get("picture"),
        is_active=True))
    log_action(db, user, "self_link_line", "user", user.id, "line_id")
    db.commit()
    _clear_history(line_user_id)   # 作用中帳號變了，AI 對話歷史不能沿用舊帳號的

    oa = settings.LINE_OA_BASIC_ID or ""
    add_friend = _ADD_FRIEND_BUTTON.format(oa=oa) if oa else ""
    is_friend = await _friend_flag(tokens.get("access_token", ""))

    # 知會同帳號既有的其他接收人。綁定此刻已經完成，所以不論新綁定者自己是不是好友
    # 都要通知——這是共用帳號的安全知會，不該因為對方沒加好友就消失。
    display = (claims or {}).get("name") or "一位成員"
    for other in others:
        await _safe_push(other, f"提醒：{display} 剛剛綁定了帳號 {user.username} 的 LINE 通知。")

    if is_friend is False:      # 明確不是好友，才擋下來提醒
        return _page("還差一步",
                     "綁定已完成，但你還沒有加入我們的官方帳號好友——沒加好友就收不到任何通知。",
                     add_friend)

    # True 或 None 都照成功走。None＝查不到好友狀態（多半是 Login channel 未連結
    # 官方帳號），此時推播可能靜默失敗，故成功頁附上「沒收到訊息就是還沒加好友」的提示。
    await _safe_push(line_user_id,
        f"✅ 綁定成功！目前作用帳號：{user.username}\n"
        f"AI 助理已可直接使用；相機開機通知還需到網頁「通知設定」逐台開啟訂閱。")

    if is_friend is True:
        return _page("✅ 已綁定完成",
                     f"帳號 {user.username} 之後會把相機通知送到這支 LINE。可以關閉此頁了。")
    return _page("✅ 已綁定完成",
                 f"帳號 {user.username} 之後會把相機通知送到這支 LINE。"
                 f"若你沒有收到我們剛送出的歡迎訊息，代表還沒加入官方帳號好友，請按下方按鈕。",
                 add_friend)
