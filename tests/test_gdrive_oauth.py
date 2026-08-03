"""GDrive OAuth redirect flow 測試。

conftest.py 的 db fixture 只建四張表，這裡自行補上本功能需要的兩張，
並自組一個只掛 jobs router 的 app（conftest 的 app fixture 只掛 auth router）。
"""
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt

from database import SessionLocal, engine
from models import GoogleDriveCredential, GDriveJob
from routers.jobs import router as jobs_router
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
