"""LINE 綁定流程改寫到 user_line_accounts：支援一帳號多綁、逐筆解綁。"""
import secrets as secrets_mod

import httpx

import auth as auth_mod
from models import UserLineAccount


class _FakeLineResponse:
    def __init__(self, data, is_success=True):
        self._data = data
        self.is_success = is_success

    def json(self):
        return self._data


class _FakeLineAsyncClient:
    """最小假 httpx.AsyncClient：只回應 line_callback 綁定分支需要的兩個呼叫。"""

    def __init__(self, user_id="line-uid-1", display_name="Bind Tester"):
        self.user_id = user_id
        self.display_name = display_name

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        assert "api.line.me/oauth2/v2.1/token" in url
        return _FakeLineResponse({"access_token": "fake-line-token"})

    async def get(self, url, **kwargs):
        assert "api.line.me/v2/profile" in url
        return _FakeLineResponse({"userId": self.user_id, "displayName": self.display_name})


def _bind(client, monkeypatch, user, line_user_id="line-uid-1", display_name="Bind Tester"):
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **kw: _FakeLineAsyncClient(line_user_id, display_name))
    ticket = auth_mod.create_line_bind_token(user.id, "/notifications")
    state = f"{secrets_mod.token_urlsafe(16)}:bind:{ticket}"
    return client.get(
        f"/auth/line/callback?code=fakecode&state={state}",
        cookies={"line_oauth_state": state},
        follow_redirects=False,
    )


def test_line_callback_bind_writes_user_line_account(client, make_user, db, monkeypatch):
    user = make_user("linebind1", "linebind1@example.com", password="oldpassword")
    r = _bind(client, monkeypatch, user, "line-uid-audit-1", "Audit Tester")
    assert r.status_code in (302, 307)
    assert "line_bind=ok" in r.headers["location"]

    rows = db.query(UserLineAccount).filter(UserLineAccount.user_id == user.id).all()
    assert len(rows) == 1
    assert rows[0].line_user_id == "line-uid-audit-1"
    assert rows[0].display_name == "Audit Tester"


def test_line_callback_bind_succeeds_without_state_cookie(client, make_user, db, monkeypatch):
    """F2：綁定連結可能在別的裝置/LINE 內建瀏覽器開啟，不會帶 bind-url 當下設的 cookie；
    只要 state 內的 ticket 能驗證通過，就該放行，不能卡在 cookie round-trip。"""
    user = make_user("linebindnocookie", "linebindnocookie@example.com", password="oldpassword")
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **kw: _FakeLineAsyncClient("line-uid-nocookie", "No Cookie"))
    ticket = auth_mod.create_line_bind_token(user.id, "/notifications")
    state = f"{secrets_mod.token_urlsafe(16)}:bind:{ticket}"

    r = client.get(
        f"/auth/line/callback?code=fakecode&state={state}",
        follow_redirects=False,
    )
    assert r.status_code in (302, 307)
    assert "line_bind=ok" in r.headers["location"]

    rows = db.query(UserLineAccount).filter(UserLineAccount.user_id == user.id).all()
    assert len(rows) == 1
    assert rows[0].line_user_id == "line-uid-nocookie"


def test_line_callback_bind_succeeds_with_mismatched_cookie(client, make_user, db, monkeypatch):
    """F2：cookie 存在但與 state 不同（例如另一個瀏覽器分頁殘留舊值）也不該擋下有效 ticket。"""
    user = make_user("linebindbadcookie", "linebindbadcookie@example.com", password="oldpassword")
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **kw: _FakeLineAsyncClient("line-uid-badcookie", "Bad Cookie"))
    ticket = auth_mod.create_line_bind_token(user.id, "/notifications")
    state = f"{secrets_mod.token_urlsafe(16)}:bind:{ticket}"

    r = client.get(
        f"/auth/line/callback?code=fakecode&state={state}",
        cookies={"line_oauth_state": "some-other-stale-state"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 307)
    assert "line_bind=ok" in r.headers["location"]


def test_line_callback_bind_invalid_ticket_redirects_error(client, make_user, monkeypatch):
    """F2：ticket 無效/過期時仍須導向 line_bind=error，不能因略過 cookie 檢查而放行。"""
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **kw: _FakeLineAsyncClient("line-uid-bogus", "Bogus"))
    state = f"{secrets_mod.token_urlsafe(16)}:bind:not-a-real-ticket"

    r = client.get(
        f"/auth/line/callback?code=fakecode&state={state}",
        follow_redirects=False,
    )
    assert r.status_code in (302, 307)
    assert "line_bind=error" in r.headers["location"]


def test_line_callback_non_bind_state_still_requires_cookie_match(client):
    """非綁定流程（現只剩 error redirect 路徑）維持原本的 cookie round-trip 驗證。"""
    state = secrets_mod.token_urlsafe(16)
    r = client.get(
        f"/auth/line/callback?code=fakecode&state={state}",
        follow_redirects=False,
    )
    assert r.status_code in (302, 307)
    assert "error=state_mismatch" in r.headers["location"]


def test_line_callback_bind_conflict_when_line_already_bound_to_other_user(
    client, make_user, db, monkeypatch
):
    owner = make_user("lineowner", "lineowner@example.com", password="oldpassword")
    other = make_user("lineother", "lineother@example.com", password="oldpassword")
    db.add(UserLineAccount(user_id=owner.id, line_user_id="line-uid-taken"))
    db.commit()

    r = _bind(client, monkeypatch, other, "line-uid-taken", "Taken")
    assert r.status_code in (302, 307)
    assert "line_bind=conflict" in r.headers["location"]

    rows = db.query(UserLineAccount).filter(UserLineAccount.line_user_id == "line-uid-taken").all()
    assert len(rows) == 1
    assert rows[0].user_id == owner.id


def test_line_callback_bind_second_line_account_for_same_user(client, make_user, db, monkeypatch):
    user = make_user("linemulti", "linemulti@example.com", password="oldpassword")
    r1 = _bind(client, monkeypatch, user, "line-uid-a", "First")
    assert "line_bind=ok" in r1.headers["location"]
    r2 = _bind(client, monkeypatch, user, "line-uid-b", "Second")
    assert "line_bind=ok" in r2.headers["location"]

    rows = db.query(UserLineAccount).filter(UserLineAccount.user_id == user.id).all()
    assert {row.line_user_id for row in rows} == {"line-uid-a", "line-uid-b"}


def test_unlink_specific_line_account_removes_only_that_one(client, make_user, auth_headers, db):
    user = make_user("lineunlink", "lineunlink@example.com", password="oldpassword")
    a = UserLineAccount(user_id=user.id, line_user_id="line-uid-x", display_name="X")
    b = UserLineAccount(user_id=user.id, line_user_id="line-uid-y", display_name="Y")
    db.add_all([a, b])
    db.commit()
    db.refresh(a)
    db.refresh(b)

    r = client.delete(f"/auth/me/line/{a.id}", headers=auth_headers(user))
    assert r.status_code == 200

    rows = db.query(UserLineAccount).filter(UserLineAccount.user_id == user.id).all()
    assert len(rows) == 1
    assert rows[0].id == b.id


def test_unlink_line_account_after_removing_all_leaves_list_empty(client, make_user, auth_headers, db):
    user = make_user("lineunlinkall", "lineunlinkall@example.com", password="oldpassword")
    a = UserLineAccount(user_id=user.id, line_user_id="line-uid-z")
    db.add(a)
    db.commit()
    db.refresh(a)

    r = client.delete(f"/auth/me/line/{a.id}", headers=auth_headers(user))
    assert r.status_code == 200

    body = client.get("/auth/me", headers=auth_headers(user)).json()
    assert body["line_accounts"] == []
    assert body["line_linked"] is False


def test_unlink_line_account_not_owned_by_user_404s(client, make_user, auth_headers, db):
    owner = make_user("lineownerx", "lineownerx@example.com", password="oldpassword")
    other = make_user("lineotherx", "lineotherx@example.com", password="oldpassword")
    a = UserLineAccount(user_id=owner.id, line_user_id="line-uid-w")
    db.add(a)
    db.commit()
    db.refresh(a)

    r = client.delete(f"/auth/me/line/{a.id}", headers=auth_headers(other))
    assert r.status_code == 404
    assert db.query(UserLineAccount).filter(UserLineAccount.id == a.id).first() is not None


def test_unlink_line_account_unknown_id_404s(client, make_user, auth_headers):
    user = make_user("lineunknown", "lineunknown@example.com", password="oldpassword")
    r = client.delete("/auth/me/line/999999", headers=auth_headers(user))
    assert r.status_code == 404


def test_me_reports_multiple_line_accounts(client, make_user, auth_headers, db):
    user = make_user("linemany", "linemany@example.com", password="oldpassword")
    db.add_all([
        UserLineAccount(user_id=user.id, line_user_id="line-uid-1", display_name="One"),
        UserLineAccount(user_id=user.id, line_user_id="line-uid-2", display_name="Two"),
    ])
    db.commit()

    body = client.get("/auth/me", headers=auth_headers(user)).json()
    assert body["line_linked"] is True
    assert len(body["line_accounts"]) == 2
    names = {a["display_name"] for a in body["line_accounts"]}
    assert names == {"One", "Two"}
    for a in body["line_accounts"]:
        assert "id" in a and "created_at" in a
