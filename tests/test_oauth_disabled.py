"""Task 3：第三方「登入」停用 — Google/LINE 登入端點一律 410，OAuth 只保留 LINE 綁定。"""
import secrets as secrets_mod

from models import User


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


def test_line_bind_url_still_works(client, make_user, auth_headers):
    """Task 2 的綁定端點不受影響。"""
    user = make_user("lb1", "lb1@example.com", password="oldpassword")
    r = client.get("/auth/line/bind-url", headers=auth_headers(user))
    assert r.status_code == 200
    assert ":bind:" in r.json()["state"]


class _FakeLineResponse:
    def __init__(self, data, is_success=True):
        self._data = data
        self.is_success = is_success

    def json(self):
        return self._data


class _FakeLineAsyncClient:
    """假 httpx.AsyncClient：模擬 line_callback 的 login（非 bind）分支。"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        assert "api.line.me/oauth2/v2.1/token" in url
        return _FakeLineResponse({"access_token": "fake-line-token"})

    async def get(self, url, **kwargs):
        assert "api.line.me/v2/profile" in url
        return _FakeLineResponse({"userId": "line-uid-disabled-1", "displayName": "Nobody"})


def test_line_callback_login_path_does_not_create_account(client, db, monkeypatch):
    """非 bind 分支（純登入）改為 redirect 帶 error=oauth_disabled，不得建帳。"""
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _FakeLineAsyncClient())

    before_count = db.query(User).count()

    state = secrets_mod.token_urlsafe(16)
    r = client.get(
        f"/auth/line/callback?code=fakecode&state={state}",
        cookies={"line_oauth_state": state},
        follow_redirects=False,
    )
    assert r.status_code in (302, 307)
    assert "error=oauth_disabled" in r.headers["location"]

    after_count = db.query(User).count()
    assert after_count == before_count
