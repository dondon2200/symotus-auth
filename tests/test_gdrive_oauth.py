"""GDrive OAuth redirect flow 測試。

conftest.py 的 db fixture 只建四張表，這裡自行補上本功能需要的兩張，
並自組一個只掛 jobs router 的 app（conftest 的 app fixture 只掛 auth router）。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database import SessionLocal, engine
from models import GoogleDriveCredential, GDriveJob
from routers.jobs import router as jobs_router


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
