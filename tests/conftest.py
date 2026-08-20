"""測試用 fixture。

兩個必要的繞道：
1. models.py 有 PostgreSQL 專屬的 ARRAY 欄位，sqlite 建不起來，所以只建本功能用到的四張表。
2. main.py 的 startup event 會 create_all + ALTER TABLE，在 sqlite 上會炸，所以自組最小 app。
"""
import os
import pathlib
import sys

# config.py 以環境變數初始化 Settings，必須在任何專案模組 import 之前設好
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
TEST_DB = pathlib.Path(__file__).resolve().parent / "test_auth.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB}"
os.environ["JWT_SECRET"] = "test-secret-for-pytest-only"
TEST_DB.unlink(missing_ok=True)

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database import SessionLocal, engine
from models import (
    User, RefreshToken, AuditLog, CameraAccess,
    BillingPlan, BillingCustomer, BillingSubscription,
    BillingInvoice, BillingInvoiceLine, BillingUsageDaily,
)
from auth import hash_password, create_access_token
from routers.auth import router as auth_router

_TABLES = [
    User.__table__, RefreshToken.__table__, AuditLog.__table__, CameraAccess.__table__,
    BillingPlan.__table__, BillingCustomer.__table__, BillingSubscription.__table__,
    BillingInvoice.__table__, BillingInvoiceLine.__table__, BillingUsageDaily.__table__,
]


@pytest.fixture()
def db():
    for t in _TABLES:
        t.drop(bind=engine, checkfirst=True)
    for t in _TABLES:
        t.create(bind=engine, checkfirst=True)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def app():
    a = FastAPI()
    a.include_router(auth_router)
    return a


@pytest.fixture()
def client(app, db):
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def make_user(db):
    def _make(username, email, password=None, google_id=None, line_id=None, role="end_user"):
        user = User(
            username=username,
            email=email,
            full_name=None,
            hashed_password=hash_password(password) if password else None,
            role=role,
            is_active=True,
            google_id=google_id,
            line_id=line_id,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user
    return _make


@pytest.fixture()
def auth_headers(db):
    def _headers(user):
        return {"Authorization": f"Bearer {create_access_token(user, db)}"}
    return _headers
