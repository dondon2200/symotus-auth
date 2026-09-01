"""綁定碼產生端點：6 位數、10 分鐘、產新碼作廢舊碼。"""
from datetime import datetime

from models import LineBindCode


def test_bind_code_created(client, make_user, auth_headers, db):
    user = make_user("bc1", "bc1@x.com", password="password123")
    r = client.post("/auth/me/line/bind-code", headers=auth_headers(user))
    assert r.status_code == 200
    body = r.json()
    assert len(body["code"]) == 6 and body["code"].isdigit()
    assert "expires_at" in body and "oa_add_url" in body
    row = db.query(LineBindCode).filter(LineBindCode.user_id == user.id).one()
    assert row.code == body["code"] and row.used_at is None
    assert (row.expires_at - datetime.utcnow()).total_seconds() > 9 * 60


def test_new_code_invalidates_old(client, make_user, auth_headers, db):
    user = make_user("bc2", "bc2@x.com", password="password123")
    c1 = client.post("/auth/me/line/bind-code", headers=auth_headers(user)).json()["code"]
    c2 = client.post("/auth/me/line/bind-code", headers=auth_headers(user)).json()["code"]
    rows = db.query(LineBindCode).filter(LineBindCode.user_id == user.id).all()
    assert len(rows) == 1 and rows[0].code == c2


def test_bind_code_requires_auth(client):
    r = client.post("/auth/me/line/bind-code")
    assert r.status_code in (401, 403)


def test_oa_add_url_from_settings(client, make_user, auth_headers, monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "LINE_OA_BASIC_ID", "@symotus")
    user = make_user("bc3", "bc3@x.com", password="password123")
    body = client.post("/auth/me/line/bind-code", headers=auth_headers(user)).json()
    assert body["oa_add_url"] == "https://line.me/R/ti/p/@symotus"
