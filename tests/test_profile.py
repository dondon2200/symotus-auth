import datetime

from models import RefreshToken


def _add_refresh(db, user, token):
    db.add(RefreshToken(user_id=user.id, token=token, revoked=False,
                        expires_at=datetime.datetime.utcnow() + datetime.timedelta(days=30)))
    db.commit()


def test_me_returns_current_user(client, make_user, auth_headers):
    user = make_user("alice", "alice@example.com", password="oldpassword")
    r = client.get("/auth/me", headers=auth_headers(user))
    assert r.status_code == 200
    assert r.json()["username"] == "alice"


def test_me_requires_token(client):
    assert client.get("/auth/me").status_code == 403


def test_me_exposes_credential_flags(client, make_user, auth_headers):
    user = make_user("bob", "bob@example.com", password="oldpassword", google_id="g-1")
    body = client.get("/auth/me", headers=auth_headers(user)).json()
    assert body["has_password"] is True
    assert body["google_linked"] is True
    assert body["line_linked"] is False
    assert body["created_at"] is not None


def test_me_flags_oauth_only_account(client, make_user, auth_headers, db):
    from models import UserLineAccount
    user = make_user("carol", "carol@example.com")
    db.add(UserLineAccount(user_id=user.id, line_user_id="l-1"))
    db.commit()
    body = client.get("/auth/me", headers=auth_headers(user)).json()
    assert body["has_password"] is False
    assert body["line_linked"] is True


def test_update_full_name(client, make_user, auth_headers, db):
    user = make_user("dave", "dave@example.com", password="oldpassword")
    r = client.put("/auth/me", headers=auth_headers(user), json={"full_name": "大衛"})
    assert r.status_code == 200
    assert r.json()["full_name"] == "大衛"
    db.refresh(user)
    assert user.full_name == "大衛"


def test_update_me_ignores_protected_fields(client, make_user, auth_headers, db):
    user = make_user("erin", "erin@example.com", password="oldpassword")
    r = client.put("/auth/me", headers=auth_headers(user), json={
        "full_name": "艾琳", "username": "hacker",
        "email": "hacker@example.com", "role": "symotus_admin",
    })
    assert r.status_code == 200
    db.refresh(user)
    assert user.username == "erin"
    assert user.email == "erin@example.com"
    assert user.role == "end_user"
    assert user.full_name == "艾琳"


def test_update_me_writes_audit_log(client, make_user, auth_headers, db):
    from models import AuditLog
    user = make_user("frank", "frank@example.com", password="oldpassword")
    client.put("/auth/me", headers=auth_headers(user), json={"full_name": "法蘭克"})
    log = db.query(AuditLog).filter(AuditLog.action == "self_update_profile").first()
    assert log is not None
    assert log.actor_id == user.id


def test_update_me_without_full_name_field_keeps_existing_name(client, make_user, auth_headers, db):
    user = make_user("felix", "felix@example.com", password="oldpassword")
    user.full_name = "菲力克斯"
    db.commit()
    r = client.put("/auth/me", headers=auth_headers(user), json={})
    assert r.status_code == 200
    assert r.json()["full_name"] == "菲力克斯"
    db.refresh(user)
    assert user.full_name == "菲力克斯"


def test_change_password_with_correct_current(client, make_user, auth_headers, db):
    from auth import verify_password
    user = make_user("gina", "gina@example.com", password="oldpassword")
    r = client.post("/auth/me/password", headers=auth_headers(user), json={
        "current_password": "oldpassword", "new_password": "newpassword1"})
    assert r.status_code == 200
    db.refresh(user)
    assert verify_password("newpassword1", user.hashed_password)


def test_change_password_rejects_wrong_current(client, make_user, auth_headers):
    user = make_user("hank", "hank@example.com", password="oldpassword")
    r = client.post("/auth/me/password", headers=auth_headers(user), json={
        "current_password": "wrongpassword", "new_password": "newpassword1"})
    assert r.status_code == 401


def test_change_password_requires_current_when_password_exists(client, make_user, auth_headers):
    user = make_user("iris", "iris@example.com", password="oldpassword")
    r = client.post("/auth/me/password", headers=auth_headers(user), json={
        "new_password": "newpassword1"})
    assert r.status_code == 401


def test_oauth_only_account_sets_password_without_current(client, make_user, auth_headers, db):
    user = make_user("jack", "jack@example.com", google_id="g-jack")
    r = client.post("/auth/me/password", headers=auth_headers(user), json={
        "new_password": "brandnewpass"})
    assert r.status_code == 200
    body = client.get("/auth/me", headers=auth_headers(user)).json()
    assert body["has_password"] is True


def test_change_password_rejects_short_password(client, make_user, auth_headers):
    user = make_user("kate", "kate@example.com", password="oldpassword")
    r = client.post("/auth/me/password", headers=auth_headers(user), json={
        "current_password": "oldpassword", "new_password": "short"})
    assert r.status_code == 400


def test_change_password_revokes_other_refresh_tokens(client, make_user, auth_headers, db):
    user = make_user("liam", "liam@example.com", password="oldpassword")
    _add_refresh(db, user, "keep-me")
    _add_refresh(db, user, "kill-me")
    client.post("/auth/me/password", headers=auth_headers(user), json={
        "current_password": "oldpassword", "new_password": "newpassword1",
        "keep_refresh_token": "keep-me"})
    kept = db.query(RefreshToken).filter(RefreshToken.token == "keep-me").first()
    killed = db.query(RefreshToken).filter(RefreshToken.token == "kill-me").first()
    assert kept.revoked is False
    assert killed.revoked is True


def test_logout_all_revokes_every_refresh_token(client, make_user, auth_headers, db):
    user = make_user("mia", "mia@example.com", password="oldpassword")
    _add_refresh(db, user, "device-a")
    _add_refresh(db, user, "device-b")
    r = client.post("/auth/me/logout-all", headers=auth_headers(user))
    assert r.status_code == 200
    tokens = db.query(RefreshToken).filter(RefreshToken.user_id == user.id).all()
    assert all(t.revoked for t in tokens)


def test_logout_all_does_not_touch_other_users(client, make_user, auth_headers, db):
    mine = make_user("nina", "nina@example.com", password="oldpassword")
    other = make_user("oscar", "oscar@example.com", password="oldpassword")
    _add_refresh(db, mine, "mine-1")
    _add_refresh(db, other, "other-1")
    client.post("/auth/me/logout-all", headers=auth_headers(mine))
    kept = db.query(RefreshToken).filter(RefreshToken.token == "other-1").first()
    assert kept.revoked is False


def test_revoked_refresh_token_cannot_refresh(client, make_user, auth_headers, db):
    user = make_user("paul", "paul@example.com", password="oldpassword")
    _add_refresh(db, user, "device-c")
    client.post("/auth/me/logout-all", headers=auth_headers(user))
    r = client.post("/auth/refresh", json={"refresh_token": "device-c"})
    assert r.status_code == 401


def test_unlink_google_disabled(client, make_user, auth_headers, db):
    """Task 3：第三方登入停用後，舊式 /me/unlink/{provider} 一律 410。"""
    user = make_user("quinn", "quinn@example.com", password="oldpassword", google_id="g-q")
    r = client.post("/auth/me/unlink/google", headers=auth_headers(user))
    assert r.status_code == 410


def test_unlink_line_disabled(client, make_user, auth_headers, db):
    user = make_user("rita", "rita@example.com", google_id="g-r", line_id="l-r")
    r = client.post("/auth/me/unlink/line", headers=auth_headers(user))
    assert r.status_code == 410


def test_unlink_unknown_provider_disabled(client, make_user, auth_headers):
    user = make_user("tina", "tina@example.com", password="oldpassword", google_id="g-t")
    r = client.post("/auth/me/unlink/facebook", headers=auth_headers(user))
    assert r.status_code == 410


def test_google_bind_url_disabled(client, make_user, auth_headers):
    """Task 3：Google OAuth 全面停用，含綁定用途。"""
    user = make_user("ulla", "ulla@example.com", password="oldpassword")
    r = client.get("/auth/google/bind-url", headers=auth_headers(user))
    assert r.status_code == 410


def test_link_google_disabled(client, make_user, auth_headers, db):
    user = make_user("vera", "vera@example.com", password="oldpassword")
    state = "r:gbind:whatever"
    r = client.post("/auth/me/link/google", headers=auth_headers(user),
                    json={"code": "c", "state": state})
    assert r.status_code == 410
    db.refresh(user)
    assert user.google_id is None


def test_line_bind_token_round_trips_next_path():
    from auth import create_line_bind_token, decode_line_bind_token
    ticket = create_line_bind_token(42, "/profile")
    assert decode_line_bind_token(ticket) == (42, "/profile")


def test_line_bind_token_defaults_to_notifications():
    from auth import create_line_bind_token, decode_line_bind_token
    ticket = create_line_bind_token(42)
    assert decode_line_bind_token(ticket) == (42, "/notifications")


def test_line_bind_token_rejects_external_next():
    from auth import create_line_bind_token, decode_line_bind_token
    ticket = create_line_bind_token(42, "https://evil.example.com")
    assert decode_line_bind_token(ticket) == (42, "/notifications")


class _FakeLineResponse:
    def __init__(self, data, is_success=True):
        self._data = data
        self.is_success = is_success

    def json(self):
        return self._data


class _FakeLineAsyncClient:
    """最小假 httpx.AsyncClient：只回應 line_callback 綁定分支需要的兩個呼叫。"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        assert "api.line.me/oauth2/v2.1/token" in url
        return _FakeLineResponse({"access_token": "fake-line-token"})

    async def get(self, url, **kwargs):
        assert "api.line.me/v2/profile" in url
        return _FakeLineResponse({"userId": "line-uid-audit-1", "displayName": "Audit Tester"})


def test_line_callback_bind_writes_audit_log(client, make_user, db, monkeypatch):
    """N-2: line_callback 的 bind_user_id 分支綁定 LINE 後必須寫入 self_link_line 稽核紀錄。"""
    import secrets as secrets_mod
    import httpx
    from models import AuditLog, UserLineAccount
    import auth as auth_mod

    user = make_user("linebind", "linebind@example.com", password="oldpassword")
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _FakeLineAsyncClient())

    ticket = auth_mod.create_line_bind_token(user.id, "/notifications")
    state = f"{secrets_mod.token_urlsafe(16)}:bind:{ticket}"

    r = client.get(
        f"/auth/line/callback?code=fakecode&state={state}",
        cookies={"line_oauth_state": state},
        follow_redirects=False,
    )
    assert r.status_code in (302, 307)
    assert "line_bind=ok" in r.headers["location"]

    row = db.query(UserLineAccount).filter(UserLineAccount.user_id == user.id).first()
    assert row is not None
    assert row.line_user_id == "line-uid-audit-1"

    log = db.query(AuditLog).filter(AuditLog.action == "self_link_line").first()
    assert log is not None
    assert log.actor_id == user.id
    assert log.target_id == user.id
