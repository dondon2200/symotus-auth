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


def test_me_flags_oauth_only_account(client, make_user, auth_headers):
    user = make_user("carol", "carol@example.com", line_id="l-1")
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


def test_unlink_google_when_password_exists(client, make_user, auth_headers, db):
    user = make_user("quinn", "quinn@example.com", password="oldpassword", google_id="g-q")
    r = client.post("/auth/me/unlink/google", headers=auth_headers(user))
    assert r.status_code == 200
    assert r.json()["google_linked"] is False
    db.refresh(user)
    assert user.google_id is None


def test_unlink_line_when_google_still_linked(client, make_user, auth_headers, db):
    user = make_user("rita", "rita@example.com", google_id="g-r", line_id="l-r")
    r = client.post("/auth/me/unlink/line", headers=auth_headers(user))
    assert r.status_code == 200
    db.refresh(user)
    assert user.line_id is None


def test_unlink_last_login_method_rejected(client, make_user, auth_headers, db):
    user = make_user("sam", "sam@example.com", google_id="g-s")
    r = client.post("/auth/me/unlink/google", headers=auth_headers(user))
    assert r.status_code == 400
    db.refresh(user)
    assert user.google_id == "g-s"


def test_unlink_unknown_provider_rejected(client, make_user, auth_headers):
    user = make_user("tina", "tina@example.com", password="oldpassword", google_id="g-t")
    r = client.post("/auth/me/unlink/facebook", headers=auth_headers(user))
    assert r.status_code == 400


def test_unlink_unlinked_provider_rejected(client, make_user, auth_headers):
    user = make_user("uuid", "uuid@example.com", password="oldpassword")
    r = client.post("/auth/me/unlink/google", headers=auth_headers(user))
    assert r.status_code == 400
    assert "尚未綁定該登入方式" in r.json()["detail"]


def test_google_bind_url_contains_bind_ticket(client, make_user, auth_headers):
    user = make_user("ulla", "ulla@example.com", password="oldpassword")
    r = client.get("/auth/google/bind-url", headers=auth_headers(user))
    assert r.status_code == 200
    assert ":gbind:" in r.json()["state"]


def test_link_google_writes_google_id(client, make_user, auth_headers, db, monkeypatch):
    import routers.auth as auth_router_mod
    import auth
    user = make_user("vera", "vera@example.com", password="oldpassword")

    async def fake_profile(code, state):
        return {"sub": "g-vera", "email": "vera@gmail.com"}
    monkeypatch.setattr(auth_router_mod, "_fetch_google_profile", fake_profile)

    state = f"r:gbind:{auth.create_google_bind_token(user.id)}"
    r = client.post("/auth/me/link/google", headers=auth_headers(user),
                    json={"code": "c", "state": state})
    assert r.status_code == 200
    assert r.json()["google_linked"] is True
    db.refresh(user)
    assert user.google_id == "g-vera"


def test_link_google_rejects_id_owned_by_someone_else(client, make_user, auth_headers, db, monkeypatch):
    import routers.auth as auth_router_mod
    import auth
    make_user("wendy", "wendy@example.com", google_id="g-taken")
    user = make_user("xavier", "xavier@example.com", password="oldpassword")

    async def fake_profile(code, state):
        return {"sub": "g-taken", "email": "taken@gmail.com"}
    monkeypatch.setattr(auth_router_mod, "_fetch_google_profile", fake_profile)

    state = f"r:gbind:{auth.create_google_bind_token(user.id)}"
    r = client.post("/auth/me/link/google", headers=auth_headers(user),
                    json={"code": "c", "state": state})
    assert r.status_code == 409
    db.refresh(user)
    assert user.google_id is None


def test_link_google_rejects_state_without_gbind_marker(client, make_user, auth_headers, db, monkeypatch):
    import routers.auth as auth_router_mod
    user = make_user("yara", "yara@example.com", password="oldpassword")

    async def fake_profile(code, state):
        return {"sub": "g-yara", "email": "yara@gmail.com"}
    monkeypatch.setattr(auth_router_mod, "_fetch_google_profile", fake_profile)

    r = client.post("/auth/me/link/google", headers=auth_headers(user),
                    json={"code": "c", "state": "just-some-random-state"})
    assert r.status_code == 400
    db.refresh(user)
    assert user.google_id is None


def test_link_google_rejects_ticket_owned_by_another_user(client, make_user, auth_headers, db, monkeypatch):
    import routers.auth as auth_router_mod
    import auth
    other = make_user("zack", "zack@example.com", password="oldpassword")
    user = make_user("amelia", "amelia@example.com", password="oldpassword")

    async def fake_profile(code, state):
        return {"sub": "g-amelia", "email": "amelia@gmail.com"}
    monkeypatch.setattr(auth_router_mod, "_fetch_google_profile", fake_profile)

    # ticket 是為 other 簽發的，但拿去打 user 的 access token
    state = f"r:gbind:{auth.create_google_bind_token(other.id)}"
    r = client.post("/auth/me/link/google", headers=auth_headers(user),
                    json={"code": "c", "state": state})
    assert r.status_code == 400
    db.refresh(other)
    assert other.google_id is None
    db.refresh(user)
    assert user.google_id is None
