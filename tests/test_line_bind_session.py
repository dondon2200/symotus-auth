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


from models import UserLineAccount
import routers.line_bind as lb


@pytest.fixture()
def fake_line(monkeypatch):
    """把三個對外 HTTP 呼叫換成可控假物件，並攔截推播。"""
    sent = []

    async def _exchange(code):
        return {"access_token": "at-" + code, "id_token": "idt-" + code}

    async def _verify(id_token):
        return {"sub": "U-line-1", "name": "測試員", "picture": "https://p/1.jpg"}

    async def _friend(access_token):
        return True

    async def _push(user_id, messages):
        sent.append((user_id, messages))

    monkeypatch.setattr(lb, "_exchange_code", _exchange)
    monkeypatch.setattr(lb, "_verify_id_token", _verify)
    monkeypatch.setattr(lb, "_friend_flag", _friend)
    monkeypatch.setattr(lb, "line_push", _push)
    return sent


def test_callback_binds_account(client, make_user, db, line_channel, fake_line):
    user = make_user("cb1", "cb1@x.com", password="password123")
    row = _make_session(db, user)
    r = client.get(f"/auth/line/callback?code=xyz&state={row.sid}")
    assert r.status_code == 200 and "已綁定" in r.text
    acc = db.query(UserLineAccount).filter_by(user_id=user.id).one()
    assert acc.line_user_id == "U-line-1"
    assert acc.display_name == "測試員" and acc.is_active is True
    db.refresh(row)
    assert row.used_at is not None


def test_callback_is_idempotent(client, make_user, db, line_channel, fake_line):
    user = make_user("cb2", "cb2@x.com", password="password123")
    db.add(UserLineAccount(user_id=user.id, line_user_id="U-line-1",
                           display_name="舊名字", is_active=True))
    db.commit()
    row = _make_session(db, user)
    r = client.get(f"/auth/line/callback?code=xyz&state={row.sid}")
    assert r.status_code == 200 and "已經綁定" in r.text
    assert db.query(UserLineAccount).filter_by(user_id=user.id).count() == 1


def test_callback_rejects_bad_state(client, make_user, db, line_channel, fake_line):
    make_user("cb3", "cb3@x.com", password="password123")
    r = client.get("/auth/line/callback?code=xyz&state=not-a-real-sid")
    assert r.status_code == 200 and "已失效" in r.text
    assert db.query(UserLineAccount).count() == 0


def test_callback_warns_when_not_friend(client, make_user, db, line_channel,
                                        fake_line, monkeypatch):
    async def _not_friend(access_token):
        return False
    monkeypatch.setattr(lb, "_friend_flag", _not_friend)
    user = make_user("cb4", "cb4@x.com", password="password123")
    row = _make_session(db, user)
    r = client.get(f"/auth/line/callback?code=xyz&state={row.sid}")
    assert "還差一步" in r.text
    assert db.query(UserLineAccount).filter_by(user_id=user.id).count() == 1
    assert fake_line == []          # 非好友不推播


def test_callback_unknown_friendship_still_succeeds(client, make_user, db, line_channel,
                                                    fake_line, monkeypatch):
    """查不到好友狀態（Login channel 未連結官方帳號）不能當成「不是好友」，
    否則連早就加過好友的人都會看到警告。"""
    async def _unknown(access_token):
        return None
    monkeypatch.setattr(lb, "_friend_flag", _unknown)
    user = make_user("cb7", "cb7@x.com", password="password123")
    row = _make_session(db, user)
    r = client.get(f"/auth/line/callback?code=xyz&state={row.sid}")
    assert "已綁定完成" in r.text and "還差一步" not in r.text
    assert "沒有收到" in r.text                      # 附上補救提示
    assert [t for t, _ in fake_line] == ["U-line-1"]  # 仍然推播歡迎訊息


def test_callback_notifies_existing_recipients(client, make_user, db, line_channel, fake_line):
    user = make_user("cb5", "cb5@x.com", password="password123")
    db.add(UserLineAccount(user_id=user.id, line_user_id="U-existing",
                           display_name="同事", is_active=True))
    db.commit()
    row = _make_session(db, user)
    client.get(f"/auth/line/callback?code=xyz&state={row.sid}")
    targets = [t for t, _ in fake_line]
    assert "U-line-1" in targets and "U-existing" in targets


def test_callback_user_cancelled(client, make_user, db, line_channel, fake_line):
    user = make_user("cb6", "cb6@x.com", password="password123")
    row = _make_session(db, user)
    r = client.get(f"/auth/line/callback?error=access_denied&state={row.sid}")
    assert "未完成" in r.text
    assert db.query(UserLineAccount).count() == 0


def test_callback_rejects_replayed_session(client, make_user, db, line_channel, fake_line):
    """session 的 used_at 機制核心不變式：已用過的連結重放要顯示已失效，不能再次綁定。"""
    user = make_user("cb8", "cb8@x.com", password="password123")
    row = _make_session(db, user, used=True)
    r = client.get(f"/auth/line/callback?code=xyz&state={row.sid}")
    assert r.status_code == 200 and "已失效" in r.text
    assert db.query(UserLineAccount).count() == 0


def test_callback_never_issues_login_token(client, make_user, db, line_channel, fake_line):
    """callback 只做綁定，絕不可發登入 token／建帳——這是本功能最重要的安全不變式。"""
    from models import RefreshToken, User
    user = make_user("cb9", "cb9@x.com", password="password123")
    row = _make_session(db, user)
    before = db.query(User).count()
    tokens_before = db.query(RefreshToken).count()
    # follow_redirects=False：萬一哪天改成用 302 把 token 帶到前端 fragment，
    # TestClient 預設會跟著轉址，讓下面的斷言看不到問題。
    r = client.get(f"/auth/line/callback?code=xyz&state={row.sid}", follow_redirects=False)
    assert r.status_code == 200
    assert "access_token" not in r.text
    assert "set-cookie" not in {k.lower() for k in r.headers.keys()}
    assert db.query(User).count() == before
    # 發登入 token 的典型副作用是同時寫一列 refresh token；沒增加才代表真的沒發。
    assert db.query(RefreshToken).count() == tokens_before


def test_callback_deactivates_other_binding_on_new_row(client, make_user, db,
                                                        line_channel, fake_line):
    """不變式：同一支 LINE（line_user_id）最多一列 is_active=True。

    user A 已經用這支 LINE 綁定；user B 換綁同一支 LINE（B 原本沒有這支 LINE 的列，
    所以走「新增列」路徑）。綁定完成後，A 那列必須被退位，只剩 B 是作用中，
    否則 LINE AI 助理仍會以 A 的身分操作相機。
    """
    user_a = make_user("cbA1", "cbA1@x.com", password="password123")
    user_b = make_user("cbB1", "cbB1@x.com", password="password123")
    db.add(UserLineAccount(user_id=user_a.id, line_user_id="U-line-1",
                           display_name="A", is_active=True))
    db.commit()

    row = _make_session(db, user_b)
    r = client.get(f"/auth/line/callback?code=xyz&state={row.sid}")
    assert r.status_code == 200

    db.expire_all()
    rows = db.query(UserLineAccount).filter_by(line_user_id="U-line-1").all()
    assert [x.user_id for x in rows if x.is_active] == [user_b.id]


def test_callback_deactivates_other_binding_on_idempotent_path(client, make_user, db,
                                                                line_channel, fake_line):
    """不變式：同一支 LINE 最多一列 is_active=True，走冪等分支時也要成立。

    user A 是目前的作用中帳號；user B 早已綁過同一支 LINE 但目前是非作用中。
    B 再走一次 callback（B 已有列，進冪等分支），完成後只剩 B 是作用中，A 要被退位。
    """
    user_a = make_user("cbA2", "cbA2@x.com", password="password123")
    user_b = make_user("cbB2", "cbB2@x.com", password="password123")
    db.add(UserLineAccount(user_id=user_a.id, line_user_id="U-line-1",
                           display_name="A", is_active=True))
    db.add(UserLineAccount(user_id=user_b.id, line_user_id="U-line-1",
                           display_name="B", is_active=False))
    db.commit()

    row = _make_session(db, user_b)
    r = client.get(f"/auth/line/callback?code=xyz&state={row.sid}")
    assert r.status_code == 200
    assert "已經綁定" in r.text

    db.expire_all()
    rows = db.query(UserLineAccount).filter_by(line_user_id="U-line-1").all()
    assert [x.user_id for x in rows if x.is_active] == [user_b.id]
