def test_me_returns_current_user(client, make_user, auth_headers):
    user = make_user("alice", "alice@example.com", password="oldpassword")
    r = client.get("/auth/me", headers=auth_headers(user))
    assert r.status_code == 200
    assert r.json()["username"] == "alice"


def test_me_requires_token(client):
    assert client.get("/auth/me").status_code == 403
