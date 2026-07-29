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


import datetime

from models import RefreshToken


def _add_refresh(db, user, token):
    db.add(RefreshToken(user_id=user.id, token=token, revoked=False,
                        expires_at=datetime.datetime.utcnow() + datetime.timedelta(days=30)))
    db.commit()


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
