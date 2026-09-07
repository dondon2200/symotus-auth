"""LINE Login 一鍵綁定：綁定 session 與 OAuth callback。"""
import secrets
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from config import settings

from models import LineBindSession


def test_bind_session_model_roundtrip(db, make_user):
    user = make_user("bs0", "bs0@x.com", password="password123")
    row = LineBindSession(sid="abc123", user_id=user.id,
                          expires_at=datetime.utcnow() + timedelta(minutes=5))
    db.add(row)
    db.commit()
    got = db.query(LineBindSession).filter_by(sid="abc123").one()
    assert got.user_id == user.id and got.used_at is None
    assert got.user.username == "bs0"


@pytest.fixture()
def line_channel(monkeypatch):
    monkeypatch.setattr(settings, "LINE_CHANNEL_ID", "2010000000")
    monkeypatch.setattr(settings, "LINE_LOGIN_CHANNEL_SECRET", "test-secret")
    monkeypatch.setattr(settings, "LINE_REDIRECT_URI",
                        "https://user.symotus.com/auth-api/auth/line/callback")


def test_create_bind_session(client, make_user, auth_headers, db, line_channel):
    user = make_user("bs1", "bs1@x.com", password="password123")
    r = client.post("/auth/me/line/bind-session", headers=auth_headers(user))
    assert r.status_code == 200
    body = r.json()
    assert body["bind_url"].endswith("/auth/line/bind-start?s=" + body["sid"])
    row = db.query(LineBindSession).filter_by(sid=body["sid"]).one()
    assert row.user_id == user.id and row.used_at is None
    assert (row.expires_at - datetime.utcnow()).total_seconds() > 4 * 60


def test_bind_session_requires_auth(client):
    assert client.post("/auth/me/line/bind-session").status_code in (401, 403)


def test_bind_session_501_without_channel(client, make_user, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "LINE_CHANNEL_ID", None)
    user = make_user("bs2", "bs2@x.com", password="password123")
    r = client.post("/auth/me/line/bind-session", headers=auth_headers(user))
    assert r.status_code == 501


def test_bind_session_writes_audit_log(client, make_user, auth_headers, db, line_channel):
    from models import AuditLog
    user = make_user("bs6", "bs6@x.com", password="password123")
    client.post("/auth/me/line/bind-session", headers=auth_headers(user))
    row = db.query(AuditLog).filter(AuditLog.action == "self_line_bind_session").one()
    assert row.actor_id == user.id
    assert row.target_id == user.id
    assert row.target_type == "user"
    assert row.detail == "line_bind_sessions"


def _make_session(db, user, minutes=5, used=False):
    row = LineBindSession(sid=secrets.token_urlsafe(8), user_id=user.id,
                          expires_at=datetime.utcnow() + timedelta(minutes=minutes),
                          used_at=datetime.utcnow() if used else None)
    db.add(row)
    db.commit()
    return row


def test_bind_start_redirects_to_line(client, make_user, db, line_channel):
    user = make_user("bs3", "bs3@x.com", password="password123")
    row = _make_session(db, user)
    r = client.get(f"/auth/line/bind-start?s={row.sid}", follow_redirects=False)
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["response_type"] == ["code"]
    assert q["client_id"] == ["2010000000"]
    assert q["state"] == [row.sid]
    assert q["scope"] == ["profile openid"]
    assert q["bot_prompt"] == ["aggressive"]


def test_bind_start_rejects_expired(client, make_user, db, line_channel):
    user = make_user("bs4", "bs4@x.com", password="password123")
    row = _make_session(db, user, minutes=-1)
    r = client.get(f"/auth/line/bind-start?s={row.sid}", follow_redirects=False)
    assert r.status_code == 200 and "已失效" in r.text


def test_bind_start_rejects_used(client, make_user, db, line_channel):
    user = make_user("bs5", "bs5@x.com", password="password123")
    row = _make_session(db, user, used=True)
    r = client.get(f"/auth/line/bind-start?s={row.sid}", follow_redirects=False)
    assert r.status_code == 200 and "已失效" in r.text


def test_page_escapes_user_content():
    """title/body 可能含使用者可控的顯示名稱，必須轉義。"""
    import routers.line_bind as lb
    r = lb._page("<script>x</script>", "名字是 <img src=x onerror=alert(1)>")
    text = r.body.decode()
    assert "<script>x</script>" not in text
    assert "&lt;script&gt;" in text
    assert "<img src=x" not in text


def test_page_does_not_escape_extra_html():
    """extra_html 是刻意允許的 HTML 片段（加好友按鈕），不可被轉義掉。"""
    import routers.line_bind as lb
    r = lb._page("標題", "內文", '<a href="https://line.me/x">加好友</a>')
    assert '<a href="https://line.me/x">' in r.body.decode()
