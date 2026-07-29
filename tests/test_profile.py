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
