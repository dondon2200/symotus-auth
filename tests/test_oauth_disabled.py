"""Task 3：第三方「登入」停用 — Google/LINE 登入端點一律 410（本檔全程斷言）。
LINE「綁定」則不同：舊 OAuth 綁定連結（/line/bind-url）先改走官方帳號綁定碼流程而停用，
後又被 Task「LINE 一鍵綁定」的 /me/line/bind-session + /line/bind-start + /line/callback
取代——/line/bind-url 整支移除（見下方 404 斷言），/line/callback 不再是 410 stub，
其完整行為測試已搬到 test_line_bind_session.py。"""


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


def test_line_bind_url_removed(client, make_user, auth_headers):
    """舊 OAuth 綁定連結端點已整支移除，改用 POST /me/line/bind-session 發連結。"""
    user = make_user("lb1", "lb1@example.com", password="oldpassword")
    r = client.get("/auth/line/bind-url", headers=auth_headers(user))
    assert r.status_code == 404
