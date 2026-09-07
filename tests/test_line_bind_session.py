"""LINE Login 一鍵綁定：綁定 session 與 OAuth callback。"""
import secrets
from datetime import datetime, timedelta

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
