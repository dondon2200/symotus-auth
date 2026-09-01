"""Task 3：第三方「登入」停用 — Google/LINE 登入端點一律 410。
LINE 綁定（原 OAuth /line/bind-url、/line/callback）另於 Task「LINE 綁定改官方帳號綁定碼流程」
一併停用，改走 POST /auth/me/line/bind-code + webhook；見本檔下方 410 斷言。"""


def test_google_url_disabled(client):
    r = client.get("/auth/google/url")
    assert r.status_code == 410


def test_google_token_disabled(client):
    r = client.post("/auth/google/token", json={"code": "c", "state": "s"})
    assert r.status_code == 410


def test_google_bind_url_disabled(client, make_user, auth_headers):
    user = make_user("gb1", "gb1@example.com", password="oldpassword")
    r = client.get("/auth/google/bind-url", headers=auth_headers(user))
    assert r.status_code == 410


def test_link_google_disabled(client, make_user, auth_headers):
    user = make_user("gl1", "gl1@example.com", password="oldpassword")
    r = client.post("/auth/me/link/google", headers=auth_headers(user),
                    json={"code": "c", "state": "s"})
    assert r.status_code == 410


def test_line_url_disabled(client):
    r = client.get("/auth/line/url")
    assert r.status_code == 410


def test_line_token_disabled(client):
    r = client.post("/auth/line/token", json={"code": "c", "state": "s"})
    assert r.status_code == 410


def test_me_unlink_disabled(client, make_user, auth_headers):
    user = make_user("ul1", "ul1@example.com", password="oldpassword")
    r = client.post("/auth/me/unlink/google", headers=auth_headers(user))
    assert r.status_code == 410


def test_line_bind_url_disabled(client, make_user, auth_headers):
    """LINE 綁定改用官方帳號綁定碼流程，舊 OAuth 綁定連結端點停用。"""
    user = make_user("lb1", "lb1@example.com", password="oldpassword")
    r = client.get("/auth/line/bind-url", headers=auth_headers(user))
    assert r.status_code == 410


def test_line_callback_disabled(client):
    """LINE OAuth callback（登入與舊綁定分支皆已停用）一律 410，不建帳、不綁定。"""
    r = client.get("/auth/line/callback?code=fakecode&state=whatever",
                    follow_redirects=False)
    assert r.status_code == 410
