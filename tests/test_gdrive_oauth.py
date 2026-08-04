"""GDrive OAuth redirect flow 測試。

conftest.py 的 db fixture 只建四張表，這裡自行補上本功能需要的兩張，
並自組一個只掛 jobs router 的 app（conftest 的 app fixture 只掛 auth router）。
"""
import json
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from database import SessionLocal, engine
from models import GoogleDriveCredential, GDriveJob
import routers.jobs as jobs_module
from routers.jobs import router as jobs_router, DRIVE_SCOPE
from auth import create_gdrive_oauth_ticket, decode_gdrive_oauth_ticket, create_google_bind_token
from config import settings


@pytest.fixture()
def gdrive_db(db):
    """在 conftest 的四張表之外，補建本功能用到的兩張表。"""
    for t in (GoogleDriveCredential.__table__, GDriveJob.__table__):
        t.drop(bind=engine, checkfirst=True)
        t.create(bind=engine, checkfirst=True)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def gdrive_client(gdrive_db):
    a = FastAPI()
    a.include_router(jobs_router)
    # base_url 必須是 https：state cookie 帶 secure=True，httpx 不會為 http 網址存下
    with TestClient(a, base_url="https://testserver") as c:
        yield c


def test_credential_table_roundtrip(gdrive_db, make_user):
    user = make_user("cred1", "cred1@example.com", password="pw")
    gdrive_db.add(GoogleDriveCredential(
        user_id=user.id, refresh_token="rt-1",
        google_email="cred1@gmail.com", scope="drive.readonly",
    ))
    gdrive_db.commit()
    row = gdrive_db.query(GoogleDriveCredential).filter_by(user_id=user.id).one()
    assert row.refresh_token == "rt-1"
    assert row.google_email == "cred1@gmail.com"
    assert row.created_at is not None


def test_credential_table_rejects_second_row_same_user(gdrive_db, make_user):
    """一位使用者一組憑證是 DB 層的不變量（user_id unique）：直接 insert 第二筆必須失敗，
    不能只靠應用層 _upsert_drive_credential 的邏輯把關。"""
    user = make_user("cred2", "cred2@example.com", password="pw")
    gdrive_db.add(GoogleDriveCredential(
        user_id=user.id, refresh_token="rt-first", google_email="cred2@gmail.com",
    ))
    gdrive_db.commit()

    gdrive_db.add(GoogleDriveCredential(
        user_id=user.id, refresh_token="rt-second", google_email="cred2b@gmail.com",
    ))
    with pytest.raises(IntegrityError):
        gdrive_db.commit()
    gdrive_db.rollback()


def test_ticket_roundtrip():
    token = create_gdrive_oauth_ticket(42)
    assert decode_gdrive_oauth_ticket(token) == 42


def test_ticket_rejects_other_purpose():
    """google_bind 的 ticket 不能拿來當 gdrive_oauth 用。"""
    assert decode_gdrive_oauth_ticket(create_google_bind_token(42)) is None


def test_ticket_rejects_expired():
    expired = jwt.encode(
        {"sub": "42", "purpose": "gdrive_oauth",
         "exp": datetime.utcnow() - timedelta(minutes=1)},
        settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM,
    )
    assert decode_gdrive_oauth_ticket(expired) is None


def test_ticket_rejects_garbage():
    assert decode_gdrive_oauth_ticket("not-a-jwt") is None


import urllib.parse


def _query(url: str) -> dict:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))


def test_oauth_url_requires_auth(gdrive_client):
    assert gdrive_client.get("/jobs/gdrive/oauth/url").status_code == 403


def test_oauth_url_returns_google_consent_url(gdrive_client, make_user, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "cid-1")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "sec-1")
    user = make_user("u4", "u4@example.com", password="pw")
    r = gdrive_client.get("/jobs/gdrive/oauth/url", headers=auth_headers(user))
    assert r.status_code == 200

    url = r.json()["auth_url"]
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    q = _query(url)
    assert q["client_id"] == "cid-1"
    assert q["redirect_uri"] == settings.GDRIVE_REDIRECT_URI
    assert q["response_type"] == "code"
    assert q["scope"] == "https://www.googleapis.com/auth/drive.readonly"
    assert q["access_type"] == "offline"
    assert q["prompt"] == "consent"

    # state 必須同時回寫成 cookie，callback 才能做 round-trip 比對
    assert gdrive_client.cookies.get("gdrive_oauth_state") == q["state"]
    # state 裡夾帶的 ticket 要解得回同一個 user
    assert decode_gdrive_oauth_ticket(q["state"]) == user.id


def test_oauth_url_requires_server_credentials(gdrive_client, make_user, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", None)
    user = make_user("u4b", "u4b@example.com", password="pw")
    r = gdrive_client.get("/jobs/gdrive/oauth/url", headers=auth_headers(user))
    assert r.status_code == 500


def _consent_url_state(client, user, auth_headers, monkeypatch):
    """跑一次 /oauth/url，取得 state 並讓 client 帶著對應 cookie。"""
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "cid-1")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "sec-1")
    r = client.get("/jobs/gdrive/oauth/url", headers=auth_headers(user))
    return _query(r.json()["auth_url"])["state"]


def test_callback_stores_credential(gdrive_client, gdrive_db, make_user, auth_headers, monkeypatch):
    user = make_user("u5", "u5@example.com", password="pw")
    state = _consent_url_state(gdrive_client, user, auth_headers, monkeypatch)

    async def fake_exchange(code, redirect_uri="postmessage"):
        assert code == "code-abc"
        assert redirect_uri == settings.GDRIVE_REDIRECT_URI
        return {"access_token": "at-1", "refresh_token": "rt-1",
                "scope": DRIVE_SCOPE, "expires_in": 3600}

    async def fake_email(access_token):
        return "u5@gmail.com"

    monkeypatch.setattr(jobs_module, "_exchange_auth_code", fake_exchange)
    monkeypatch.setattr(jobs_module, "_fetch_google_email", fake_email)

    r = gdrive_client.get(f"/jobs/gdrive/oauth/callback?code=code-abc&state={state}",
                          follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == f"{settings.FRONTEND_URL}/gdrive?gdrive=connected"

    row = gdrive_db.query(GoogleDriveCredential).filter_by(user_id=user.id).one()
    assert row.refresh_token == "rt-1"
    assert row.google_email == "u5@gmail.com"


def test_callback_upserts_existing_credential(gdrive_client, gdrive_db, make_user, auth_headers, monkeypatch):
    user = make_user("u5b", "u5b@example.com", password="pw")
    gdrive_db.add(GoogleDriveCredential(user_id=user.id, refresh_token="old-rt"))
    gdrive_db.commit()
    state = _consent_url_state(gdrive_client, user, auth_headers, monkeypatch)

    async def fake_exchange(code, redirect_uri="postmessage"):
        return {"access_token": "at-2", "refresh_token": "new-rt", "scope": DRIVE_SCOPE}

    async def fake_email(access_token):
        return None

    monkeypatch.setattr(jobs_module, "_exchange_auth_code", fake_exchange)
    monkeypatch.setattr(jobs_module, "_fetch_google_email", fake_email)

    gdrive_client.get(f"/jobs/gdrive/oauth/callback?code=c&state={state}", follow_redirects=False)

    rows = gdrive_db.query(GoogleDriveCredential).filter_by(user_id=user.id).all()
    assert len(rows) == 1          # upsert，不是新增第二筆
    gdrive_db.refresh(rows[0])
    assert rows[0].refresh_token == "new-rt"
    # re-consent 覆蓋舊 refresh_token 時「不應」撤銷：revoke 撤的是整組 grant，
    # 撤了會讓剛拿到的新 token 一起失效（invalid_grant）。這裡沒有 monkeypatch
    # _revoke_google_token，若 upsert 仍呼叫它就會打真的網路請求而在測試環境炸掉，
    # 藉此保證 upsert 路徑不再發出撤銷呼叫。


def test_callback_rejects_state_mismatch(gdrive_client, gdrive_db, make_user, auth_headers, monkeypatch):
    user = make_user("u5c", "u5c@example.com", password="pw")
    _consent_url_state(gdrive_client, user, auth_headers, monkeypatch)
    forged = create_gdrive_oauth_ticket(user.id)   # 合法簽章，但與 cookie 不同

    r = gdrive_client.get(f"/jobs/gdrive/oauth/callback?code=c&state={forged}",
                          follow_redirects=False)
    assert r.headers["location"] == f"{settings.FRONTEND_URL}/gdrive?gdrive=error&reason=state"
    assert gdrive_db.query(GoogleDriveCredential).count() == 0


def test_callback_user_cancelled(gdrive_client, gdrive_db, make_user, auth_headers, monkeypatch):
    user = make_user("u5d", "u5d@example.com", password="pw")
    state = _consent_url_state(gdrive_client, user, auth_headers, monkeypatch)

    r = gdrive_client.get(f"/jobs/gdrive/oauth/callback?error=access_denied&state={state}",
                          follow_redirects=False)
    assert r.headers["location"] == f"{settings.FRONTEND_URL}/gdrive?gdrive=cancelled"
    assert gdrive_db.query(GoogleDriveCredential).count() == 0


def test_callback_exchange_failure(gdrive_client, gdrive_db, make_user, auth_headers, monkeypatch):
    user = make_user("u5e", "u5e@example.com", password="pw")
    state = _consent_url_state(gdrive_client, user, auth_headers, monkeypatch)

    async def boom(code, redirect_uri="postmessage"):
        raise HTTPException(400, "Google 授權碼交換失敗（400）")

    monkeypatch.setattr(jobs_module, "_exchange_auth_code", boom)

    r = gdrive_client.get(f"/jobs/gdrive/oauth/callback?code=c&state={state}",
                          follow_redirects=False)
    assert r.headers["location"] == f"{settings.FRONTEND_URL}/gdrive?gdrive=error&reason=exchange"
    assert gdrive_db.query(GoogleDriveCredential).count() == 0


def _cookie_deletion_present(response) -> bool:
    """檢查 set-cookie 標頭裡有沒有清除 gdrive_oauth_state 的那一筆，且保留原本的安全旗標。"""
    for value in response.headers.get_list("set-cookie"):
        if not value.startswith("gdrive_oauth_state="):
            continue
        expired = "Max-Age=0" in value or 'expires=Thu, 01 Jan 1970' in value.replace("Expires", "expires")
        if not expired:
            continue
        lowered = value.lower()
        if "httponly" in lowered and "secure" in lowered and "samesite=lax" in lowered:
            return True
    return False


def test_callback_no_refresh_token_writes_nothing(gdrive_client, gdrive_db, make_user, auth_headers, monkeypatch):
    user = make_user("u5f", "u5f@example.com", password="pw")
    state = _consent_url_state(gdrive_client, user, auth_headers, monkeypatch)

    async def fake_exchange(code, redirect_uri="postmessage"):
        return {"access_token": "at-only", "scope": DRIVE_SCOPE}  # 沒有 refresh_token

    monkeypatch.setattr(jobs_module, "_exchange_auth_code", fake_exchange)

    r = gdrive_client.get(f"/jobs/gdrive/oauth/callback?code=c&state={state}",
                          follow_redirects=False)
    assert r.headers["location"] == f"{settings.FRONTEND_URL}/gdrive?gdrive=error&reason=no_refresh_token"
    assert gdrive_db.query(GoogleDriveCredential).count() == 0


def test_callback_clears_state_cookie_on_success(gdrive_client, gdrive_db, make_user, auth_headers, monkeypatch):
    user = make_user("u5g", "u5g@example.com", password="pw")
    state = _consent_url_state(gdrive_client, user, auth_headers, monkeypatch)

    async def fake_exchange(code, redirect_uri="postmessage"):
        return {"access_token": "at-1", "refresh_token": "rt-1", "scope": DRIVE_SCOPE}

    async def fake_email(access_token):
        return None

    monkeypatch.setattr(jobs_module, "_exchange_auth_code", fake_exchange)
    monkeypatch.setattr(jobs_module, "_fetch_google_email", fake_email)

    r = gdrive_client.get(f"/jobs/gdrive/oauth/callback?code=c&state={state}",
                          follow_redirects=False)
    assert r.status_code == 307
    assert _cookie_deletion_present(r)


def test_callback_clears_state_cookie_on_failure(gdrive_client, gdrive_db, make_user, auth_headers, monkeypatch):
    user = make_user("u5h", "u5h@example.com", password="pw")
    state = _consent_url_state(gdrive_client, user, auth_headers, monkeypatch)

    async def boom(code, redirect_uri="postmessage"):
        raise HTTPException(400, "boom")

    monkeypatch.setattr(jobs_module, "_exchange_auth_code", boom)

    r = gdrive_client.get(f"/jobs/gdrive/oauth/callback?code=c&state={state}",
                          follow_redirects=False)
    assert _cookie_deletion_present(r)


def test_callback_non_ascii_state_redirects_to_reason_state(gdrive_client, gdrive_db, make_user, auth_headers, monkeypatch):
    """secrets.compare_digest 只吃 ASCII str/bytes；非 ASCII state 不該讓 500 洩漏，應走 reason=state。"""
    user = make_user("u5i", "u5i@example.com", password="pw")
    _consent_url_state(gdrive_client, user, auth_headers, monkeypatch)

    r = gdrive_client.get("/jobs/gdrive/oauth/callback?code=c&state=%C3%BC",
                          follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == f"{settings.FRONTEND_URL}/gdrive?gdrive=error&reason=state"
    assert gdrive_db.query(GoogleDriveCredential).count() == 0


def test_callback_store_failure_redirects_to_reason_store(gdrive_client, gdrive_db, make_user, auth_headers, monkeypatch):
    user = make_user("u5j", "u5j@example.com", password="pw")
    state = _consent_url_state(gdrive_client, user, auth_headers, monkeypatch)

    async def fake_exchange(code, redirect_uri="postmessage"):
        return {"access_token": "at-1", "refresh_token": "rt-1", "scope": DRIVE_SCOPE}

    async def fake_email(access_token):
        return None

    async def boom_upsert(db, user_id, refresh_token, google_email, scope):
        raise IntegrityError("insert", {}, Exception("unique(user_id)"))

    monkeypatch.setattr(jobs_module, "_exchange_auth_code", fake_exchange)
    monkeypatch.setattr(jobs_module, "_fetch_google_email", fake_email)
    monkeypatch.setattr(jobs_module, "_upsert_drive_credential", boom_upsert)

    r = gdrive_client.get(f"/jobs/gdrive/oauth/callback?code=c&state={state}",
                          follow_redirects=False)
    assert r.headers["location"] == f"{settings.FRONTEND_URL}/gdrive?gdrive=error&reason=store"
    assert _cookie_deletion_present(r)
    assert gdrive_db.query(GoogleDriveCredential).count() == 0


class _FakeGoogleResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text

    def json(self):
        return {"access_token": "at-fake", "expires_in": 3600}


def _patch_google_token_status(monkeypatch, status_code: int, text: str = ""):
    """讓 _refresh_access_token 內部呼叫 Google token endpoint 時，回傳指定的狀態碼，
    藉此驗證真正的分類邏輯（而不是繞過它直接假造 _refresh_access_token 本身）。"""
    class _FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return _FakeGoogleResponse(status_code, text)

    monkeypatch.setattr(jobs_module.httpx, "AsyncClient", _FakeAsyncClient)


@pytest.mark.parametrize("status_code", [400, 401])
def test_refresh_access_token_rejection_raises_runtime_error(monkeypatch, status_code):
    """400/401（如 invalid_grant）代表真正被拒絕：refresh token 已死。"""
    _patch_google_token_status(monkeypatch, status_code, "invalid_grant")
    import asyncio
    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(jobs_module._refresh_access_token("rt-x"))
    assert not isinstance(exc_info.value, jobs_module.TransientGoogleError)


@pytest.mark.parametrize("status_code", [403, 429, 500, 502, 503])
def test_refresh_access_token_transient_raises_transient_error(monkeypatch, status_code):
    """403（quota/使用者速率限制）/429/5xx 是 Google 端暫時性問題，不代表授權已死。"""
    _patch_google_token_status(monkeypatch, status_code, "server busy")
    import asyncio
    with pytest.raises(jobs_module.TransientGoogleError):
        asyncio.run(jobs_module._refresh_access_token("rt-x"))


@pytest.mark.parametrize("status_code,expected_http_status", [(400, 409), (429, 503), (500, 503)])
def test_picker_token_endpoint_classifies_google_status(
    gdrive_client, gdrive_db, make_user, auth_headers, monkeypatch, status_code, expected_http_status,
):
    """picker-token endpoint：Google 400 -> 409（授權已死），429/500 -> 503（暫時不可達）。"""
    user = make_user(f"pt{status_code}", f"pt{status_code}@example.com", password="pw")
    gdrive_db.add(GoogleDriveCredential(user_id=user.id, refresh_token=f"rt-pt-{status_code}"))
    gdrive_db.commit()
    _patch_google_token_status(monkeypatch, status_code)

    r = gdrive_client.post("/jobs/gdrive/oauth/token", headers=auth_headers(user))
    assert r.status_code == expected_http_status


@pytest.mark.parametrize("status_code,expected_http_status", [(400, 409), (429, 503), (500, 503)])
def test_create_job_bound_credential_classifies_google_status(
    gdrive_client, gdrive_db, make_user, auth_headers, monkeypatch, status_code, expected_http_status,
):
    """job 建立（綁定憑證路徑）：Google 400 -> 409（授權已死），429/500 -> 503（暫時不可達）。"""
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "sec")
    user = make_user(f"cj{status_code}", f"cj{status_code}@example.com", password="pw")
    gdrive_db.add(GoogleDriveCredential(user_id=user.id, refresh_token=f"rt-cj-{status_code}"))
    gdrive_db.commit()

    async def fake_pipeline(*a, **kw):
        pytest.fail("不該啟動 pipeline")

    monkeypatch.setattr(jobs_module, "_run_gdrive_nas_pipeline", fake_pipeline)
    _patch_google_token_status(monkeypatch, status_code)

    r = gdrive_client.post("/jobs/gdrive", headers=auth_headers(user), json=_job_body())
    assert r.status_code == expected_http_status
    assert gdrive_db.query(GDriveJob).filter_by(user_id=user.id).count() == 0


def _job_body(**over):
    body = {"folder_ids": ["folder-1"], "selection_name": "1 個資料夾", "fps": 30}
    body.update(over)
    return body


def test_create_job_without_credential_rejected(gdrive_client, make_user, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "sec")
    user = make_user("u7", "u7@example.com", password="pw")
    r = gdrive_client.post("/jobs/gdrive", headers=auth_headers(user), json=_job_body())
    assert r.status_code == 400
    assert "尚未連接" in r.json()["detail"]


def test_create_job_uses_bound_credential(gdrive_client, gdrive_db, make_user, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "sec")
    user = make_user("u7b", "u7b@example.com", password="pw")
    gdrive_db.add(GoogleDriveCredential(user_id=user.id, refresh_token="rt-7b"))
    gdrive_db.commit()

    async def fake_refresh(refresh_token):
        assert refresh_token == "rt-7b"
        return {"access_token": "at-7b", "expires_in": 3600}

    started = {}

    async def fake_pipeline(*a, **kw):
        started["args"] = a

    monkeypatch.setattr(jobs_module, "_refresh_access_token", fake_refresh)
    monkeypatch.setattr(jobs_module, "_run_gdrive_nas_pipeline", fake_pipeline)

    r = gdrive_client.post("/jobs/gdrive", headers=auth_headers(user), json=_job_body())
    assert r.status_code == 200

    job = gdrive_db.query(GDriveJob).filter_by(user_id=user.id).one()
    assert job.google_refresh_token == "rt-7b"

    # _run_gdrive_nas_pipeline(job_id, folder_ids, picked_files, refresh_token, fps,
    #                          resolution, rain_fog, darkness, max_images,
    #                          initial_access_token, initial_expires_in, ...)
    args = started["args"]
    assert args[3] == "rt-7b"     # refresh_token：不能被 access_token 頂替
    assert args[9] == "at-7b"     # initial_access_token：不能被 refresh_token 頂替
    assert args[10] == 3600       # initial_expires_in：不能悄悄留 0


def test_create_job_still_accepts_auth_code(gdrive_client, gdrive_db, make_user, auth_headers, monkeypatch):
    """雙軌相容：舊前端仍會送 auth_code，必須照舊路徑走。"""
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "sec")
    user = make_user("u7c", "u7c@example.com", password="pw")

    async def fake_exchange(code, redirect_uri="postmessage"):
        assert code == "legacy-code"
        assert redirect_uri == "postmessage"
        return {"access_token": "at-7c", "refresh_token": "rt-7c", "expires_in": 3600}

    started = {}

    async def fake_pipeline(*a, **kw):
        started["args"] = a

    monkeypatch.setattr(jobs_module, "_exchange_auth_code", fake_exchange)
    monkeypatch.setattr(jobs_module, "_run_gdrive_nas_pipeline", fake_pipeline)

    r = gdrive_client.post("/jobs/gdrive", headers=auth_headers(user),
                           json=_job_body(auth_code="legacy-code"))
    assert r.status_code == 200

    job = gdrive_db.query(GDriveJob).filter_by(user_id=user.id).one()
    assert job.google_refresh_token == "rt-7c"

    args = started["args"]
    assert args[3] == "rt-7c"
    assert args[9] == "at-7c"
    assert args[10] == 3600


def test_create_job_new_path_refresh_token_dead_returns_409(
    gdrive_client, gdrive_db, make_user, auth_headers, monkeypatch,
):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "sec")
    user = make_user("u7d", "u7d@example.com", password="pw")
    gdrive_db.add(GoogleDriveCredential(user_id=user.id, refresh_token="rt-7d"))
    gdrive_db.commit()

    async def dead_refresh(refresh_token):
        raise RuntimeError("refresh token 失效（400）")

    async def fake_pipeline(*a, **kw):
        pytest.fail("不該啟動 pipeline")

    monkeypatch.setattr(jobs_module, "_refresh_access_token", dead_refresh)
    monkeypatch.setattr(jobs_module, "_run_gdrive_nas_pipeline", fake_pipeline)

    r = gdrive_client.post("/jobs/gdrive", headers=auth_headers(user), json=_job_body())
    assert r.status_code == 409
    assert r.json()["detail"] == "Google 授權已失效，請重新連接 Google Drive"
    assert gdrive_db.query(GDriveJob).filter_by(user_id=user.id).count() == 0


def test_create_job_new_path_network_error_returns_503(
    gdrive_client, gdrive_db, make_user, auth_headers, monkeypatch,
):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "sec")
    user = make_user("u7e", "u7e@example.com", password="pw")
    gdrive_db.add(GoogleDriveCredential(user_id=user.id, refresh_token="rt-7e"))
    gdrive_db.commit()

    async def flaky_refresh(refresh_token):
        raise httpx.ConnectError("connection refused")

    async def fake_pipeline(*a, **kw):
        pytest.fail("不該啟動 pipeline")

    monkeypatch.setattr(jobs_module, "_refresh_access_token", flaky_refresh)
    monkeypatch.setattr(jobs_module, "_run_gdrive_nas_pipeline", fake_pipeline)

    r = gdrive_client.post("/jobs/gdrive", headers=auth_headers(user), json=_job_body())
    assert r.status_code == 503
    assert gdrive_db.query(GDriveJob).filter_by(user_id=user.id).count() == 0


def test_create_job_new_path_unexpected_error_propagates(
    gdrive_client, gdrive_db, make_user, auth_headers, monkeypatch,
):
    """既不是 RuntimeError 也不是 httpx 錯誤的例外，是真正的 bug，不該被偽裝成 409/503。"""
    user = make_user("u7f", "u7f@example.com", password="pw")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "sec")
    gdrive_db.add(GoogleDriveCredential(user_id=user.id, refresh_token="rt-7f"))
    gdrive_db.commit()

    async def buggy_refresh(refresh_token):
        raise ValueError("unexpected bug")

    async def fake_pipeline(*a, **kw):
        pytest.fail("不該啟動 pipeline")

    monkeypatch.setattr(jobs_module, "_refresh_access_token", buggy_refresh)
    monkeypatch.setattr(jobs_module, "_run_gdrive_nas_pipeline", fake_pipeline)

    with pytest.raises(ValueError, match="unexpected bug"):
        gdrive_client.post("/jobs/gdrive", headers=auth_headers(user), json=_job_body())

    assert gdrive_db.query(GDriveJob).filter_by(user_id=user.id).count() == 0


def test_status_not_connected(gdrive_client, make_user, auth_headers):
    user = make_user("u6", "u6@example.com", password="pw")
    r = gdrive_client.get("/jobs/gdrive/oauth/status", headers=auth_headers(user))
    assert r.status_code == 200
    assert r.json() == {"connected": False, "google_email": None}


def test_status_connected(gdrive_client, gdrive_db, make_user, auth_headers):
    user = make_user("u6b", "u6b@example.com", password="pw")
    gdrive_db.add(GoogleDriveCredential(user_id=user.id, refresh_token="rt",
                                        google_email="u6b@gmail.com"))
    gdrive_db.commit()
    body = gdrive_client.get("/jobs/gdrive/oauth/status", headers=auth_headers(user)).json()
    assert body == {"connected": True, "google_email": "u6b@gmail.com"}


def test_picker_token_requires_credential(gdrive_client, make_user, auth_headers):
    user = make_user("u6c", "u6c@example.com", password="pw")
    r = gdrive_client.post("/jobs/gdrive/oauth/token", headers=auth_headers(user))
    assert r.status_code == 404


def test_picker_token_returns_access_token(gdrive_client, gdrive_db, make_user, auth_headers, monkeypatch):
    user = make_user("u6d", "u6d@example.com", password="pw")
    gdrive_db.add(GoogleDriveCredential(user_id=user.id, refresh_token="rt-6d"))
    gdrive_db.commit()

    async def fake_refresh(refresh_token):
        assert refresh_token == "rt-6d"
        return {"access_token": "at-6d", "expires_in": 3599}

    monkeypatch.setattr(jobs_module, "_refresh_access_token", fake_refresh)

    body = gdrive_client.post("/jobs/gdrive/oauth/token", headers=auth_headers(user)).json()
    assert body == {"access_token": "at-6d", "expires_in": 3599}


def test_picker_token_revoked_refresh_token(gdrive_client, gdrive_db, make_user, auth_headers, monkeypatch):
    user = make_user("u6e", "u6e@example.com", password="pw")
    gdrive_db.add(GoogleDriveCredential(user_id=user.id, refresh_token="dead"))
    gdrive_db.commit()

    async def dead_refresh(refresh_token):
        raise RuntimeError("refresh token 失效（400）")

    monkeypatch.setattr(jobs_module, "_refresh_access_token", dead_refresh)

    r = gdrive_client.post("/jobs/gdrive/oauth/token", headers=auth_headers(user))
    assert r.status_code == 409
    assert r.json()["detail"] == "Google 授權已失效，請重新連接 Google Drive"


def test_picker_token_network_error_returns_503(gdrive_client, gdrive_db, make_user, auth_headers, monkeypatch):
    """網路暫時連不上 Google（httpx 錯誤）不該被誤判成授權已失效，要回 503 而非 409。"""
    user = make_user("u6h", "u6h@example.com", password="pw")
    gdrive_db.add(GoogleDriveCredential(user_id=user.id, refresh_token="rt-6h"))
    gdrive_db.commit()

    async def flaky_refresh(refresh_token):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(jobs_module, "_refresh_access_token", flaky_refresh)

    r = gdrive_client.post("/jobs/gdrive/oauth/token", headers=auth_headers(user))
    assert r.status_code == 503


def test_picker_token_unexpected_error_propagates_as_500(
    gdrive_client, gdrive_db, make_user, auth_headers, monkeypatch,
):
    """既不是 RuntimeError 也不是 httpx 錯誤的例外，是真正的 bug，不該被偽裝成 409。"""
    user = make_user("u6i", "u6i@example.com", password="pw")
    gdrive_db.add(GoogleDriveCredential(user_id=user.id, refresh_token="rt-6i"))
    gdrive_db.commit()

    async def buggy_refresh(refresh_token):
        raise ValueError("unexpected bug")

    monkeypatch.setattr(jobs_module, "_refresh_access_token", buggy_refresh)

    # TestClient 預設會把伺服器端未攔截的例外重新拋出（而非包成 500 回應），
    # 這正是我們要驗證的：非 RuntimeError/httpx 錯誤不能被吞掉偽裝成 409。
    with pytest.raises(ValueError, match="unexpected bug"):
        gdrive_client.post("/jobs/gdrive/oauth/token", headers=auth_headers(user))


def test_status_cross_user_isolation(gdrive_client, gdrive_db, make_user, auth_headers):
    user_a = make_user("u6j_a", "u6j_a@example.com", password="pw")
    user_b = make_user("u6j_b", "u6j_b@example.com", password="pw")
    gdrive_db.add(GoogleDriveCredential(user_id=user_a.id, refresh_token="rt-a",
                                        google_email="a@gmail.com"))
    gdrive_db.commit()

    body_b = gdrive_client.get("/jobs/gdrive/oauth/status", headers=auth_headers(user_b)).json()
    assert body_b == {"connected": False, "google_email": None}

    body_a = gdrive_client.get("/jobs/gdrive/oauth/status", headers=auth_headers(user_a)).json()
    assert body_a == {"connected": True, "google_email": "a@gmail.com"}


def test_picker_token_cross_user_isolation(gdrive_client, gdrive_db, make_user, auth_headers, monkeypatch):
    user_a = make_user("u6k_a", "u6k_a@example.com", password="pw")
    user_b = make_user("u6k_b", "u6k_b@example.com", password="pw")
    gdrive_db.add(GoogleDriveCredential(user_id=user_a.id, refresh_token="rt-a-token"))
    gdrive_db.add(GoogleDriveCredential(user_id=user_b.id, refresh_token="rt-b-token"))
    gdrive_db.commit()

    seen_tokens = []

    async def fake_refresh(refresh_token):
        seen_tokens.append(refresh_token)
        return {"access_token": f"at-for-{refresh_token}", "expires_in": 3600}

    monkeypatch.setattr(jobs_module, "_refresh_access_token", fake_refresh)

    body_b = gdrive_client.post("/jobs/gdrive/oauth/token", headers=auth_headers(user_b)).json()
    assert seen_tokens == ["rt-b-token"]
    assert body_b == {"access_token": "at-for-rt-b-token", "expires_in": 3600}


def test_disconnect_cross_user_isolation(gdrive_client, gdrive_db, make_user, auth_headers, monkeypatch):
    user_a = make_user("u6l_a", "u6l_a@example.com", password="pw")
    user_b = make_user("u6l_b", "u6l_b@example.com", password="pw")
    gdrive_db.add(GoogleDriveCredential(user_id=user_a.id, refresh_token="rt-a-keep"))
    gdrive_db.add(GoogleDriveCredential(user_id=user_b.id, refresh_token="rt-b-remove"))
    gdrive_db.commit()

    async def fake_revoke(token):
        pass

    monkeypatch.setattr(jobs_module, "_revoke_google_token", fake_revoke)

    r = gdrive_client.delete("/jobs/gdrive/oauth", headers=auth_headers(user_b))
    assert r.status_code == 200
    assert r.json() == {"revoked": True}

    assert gdrive_db.query(GoogleDriveCredential).filter_by(user_id=user_b.id).count() == 0
    remaining = gdrive_db.query(GoogleDriveCredential).filter_by(user_id=user_a.id).first()
    assert remaining is not None
    assert remaining.refresh_token == "rt-a-keep"


def test_disconnect_deletes_credential(gdrive_client, gdrive_db, make_user, auth_headers, monkeypatch):
    user = make_user("u6f", "u6f@example.com", password="pw")
    gdrive_db.add(GoogleDriveCredential(user_id=user.id, refresh_token="rt-6f"))
    gdrive_db.commit()

    called = {}

    async def fake_revoke(token):
        called["token"] = token

    monkeypatch.setattr(jobs_module, "_revoke_google_token", fake_revoke)

    r = gdrive_client.delete("/jobs/gdrive/oauth", headers=auth_headers(user))
    assert r.status_code == 200
    assert called["token"] == "rt-6f"
    assert gdrive_db.query(GoogleDriveCredential).filter_by(user_id=user.id).count() == 0


def test_disconnect_when_not_connected(gdrive_client, make_user, auth_headers):
    user = make_user("u6g", "u6g@example.com", password="pw")
    assert gdrive_client.delete("/jobs/gdrive/oauth", headers=auth_headers(user)).status_code == 200


def test_callback_exchange_non_json_body_redirects_to_reason_exchange(
    gdrive_client, gdrive_db, make_user, auth_headers, monkeypatch,
):
    """_exchange_auth_code 對 200 但非 JSON 的 body 會拋 json.JSONDecodeError（ValueError 的子類）；
    這不該外洩成裸 500，要跟其他交換失敗一樣走 reason=exchange。"""
    user = make_user("u5k", "u5k@example.com", password="pw")
    state = _consent_url_state(gdrive_client, user, auth_headers, monkeypatch)

    async def bad_json(code, redirect_uri="postmessage"):
        raise json.JSONDecodeError("Expecting value", "<html>captive portal</html>", 0)

    monkeypatch.setattr(jobs_module, "_exchange_auth_code", bad_json)

    r = gdrive_client.get(f"/jobs/gdrive/oauth/callback?code=c&state={state}",
                          follow_redirects=False)
    assert r.headers["location"] == f"{settings.FRONTEND_URL}/gdrive?gdrive=error&reason=exchange"
    assert gdrive_db.query(GoogleDriveCredential).count() == 0


def test_callback_exchange_non_dict_body_redirects_to_reason_exchange(
    gdrive_client, gdrive_db, make_user, auth_headers, monkeypatch,
):
    """_exchange_auth_code 若拿到合法 JSON 但不是物件（例如陣列），
    td.get(...) 會炸 AttributeError；同樣要當成交換失敗處理。"""
    user = make_user("u5l", "u5l@example.com", password="pw")
    state = _consent_url_state(gdrive_client, user, auth_headers, monkeypatch)

    async def returns_list(code, redirect_uri="postmessage"):
        return ["unexpected", "list"]

    monkeypatch.setattr(jobs_module, "_exchange_auth_code", returns_list)

    r = gdrive_client.get(f"/jobs/gdrive/oauth/callback?code=c&state={state}",
                          follow_redirects=False)
    assert r.headers["location"] == f"{settings.FRONTEND_URL}/gdrive?gdrive=error&reason=exchange"
    assert gdrive_db.query(GoogleDriveCredential).count() == 0


def test_callback_store_failure_rolls_back_pending_insert(
    gdrive_client, gdrive_db, make_user, auth_headers, monkeypatch,
):
    """先前的測試在 db.add() 之前就丟例外，count()==0 不論 rollback 有沒有跑都成立。
    這裡改成走真正的 _upsert_drive_credential（真的 db.add()），只在 commit 時失敗，
    確保失敗當下有一筆 pending insert，藉此驗證 db.rollback() 真的有作用。"""
    user = make_user("u5m", "u5m@example.com", password="pw")
    state = _consent_url_state(gdrive_client, user, auth_headers, monkeypatch)

    async def fake_exchange(code, redirect_uri="postmessage"):
        return {"access_token": "at-1", "refresh_token": "rt-1", "scope": DRIVE_SCOPE}

    async def fake_email(access_token):
        return None

    monkeypatch.setattr(jobs_module, "_exchange_auth_code", fake_exchange)
    monkeypatch.setattr(jobs_module, "_fetch_google_email", fake_email)

    def failing_commit(self):
        raise IntegrityError("insert", {}, Exception("unique(user_id)"))

    monkeypatch.setattr(OrmSession, "commit", failing_commit)

    r = gdrive_client.get(f"/jobs/gdrive/oauth/callback?code=c&state={state}",
                          follow_redirects=False)

    monkeypatch.undo()  # 儘早還原 Session.commit，後面才能正常查詢

    assert r.headers["location"] == f"{settings.FRONTEND_URL}/gdrive?gdrive=error&reason=store"
    assert gdrive_db.query(GoogleDriveCredential).count() == 0
