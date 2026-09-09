"""POST /admin/users/{id}/password：symotus_admin 替他人重設密碼（免舊密碼）。"""
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI

from routers.admin import router as admin_router
from auth import verify_password
from models import User, RefreshToken, AuditLog


@pytest.fixture()
def app():
    a = FastAPI()
    a.include_router(admin_router)
    return a


@pytest.fixture()
def admin(make_user):
    return make_user("plat_admin", "plat_admin@test.com", password="adminpass1", role="symotus_admin")


@pytest.fixture()
def target(make_user):
    return make_user("joseph_admin", "joseph_admin@test.com", password="oldpass123", role="symotus_admin")


def test_admin重設他人密碼_新密碼可驗證且舊密碼失效(client, admin, target, auth_headers, db):
    r = client.post(f"/admin/users/{target.id}/password",
                    headers=auth_headers(admin), json={"new_password": "a123456789"})
    assert r.status_code == 200
    db.refresh(target)
    assert verify_password("a123456789", target.hashed_password)
    assert not verify_password("oldpass123", target.hashed_password)


def test_重設後目標使用者的refresh_token全部作廢(client, admin, target, auth_headers, db):
    db.add(RefreshToken(user_id=target.id, token="rt-target",
                        expires_at=datetime.utcnow() + timedelta(days=1)))
    db.add(RefreshToken(user_id=admin.id, token="rt-admin",
                        expires_at=datetime.utcnow() + timedelta(days=1)))
    db.commit()
    r = client.post(f"/admin/users/{target.id}/password",
                    headers=auth_headers(admin), json={"new_password": "a123456789"})
    assert r.status_code == 200
    assert db.query(RefreshToken).filter(RefreshToken.token == "rt-target").one().revoked is True
    assert db.query(RefreshToken).filter(RefreshToken.token == "rt-admin").one().revoked is False


def test_重設會寫稽核紀錄(client, admin, target, auth_headers, db):
    client.post(f"/admin/users/{target.id}/password",
                headers=auth_headers(admin), json={"new_password": "a123456789"})
    log = db.query(AuditLog).filter(AuditLog.action == "admin_reset_password").first()
    assert log is not None
    assert log.actor_id == admin.id
    assert log.target_id == target.id


def test_非symotus_admin不可重設(client, make_user, target, auth_headers, db):
    reseller = make_user("rs", "rs@test.com", password="resellerpw1", role="reseller")
    r = client.post(f"/admin/users/{target.id}/password",
                    headers=auth_headers(reseller), json={"new_password": "a123456789"})
    assert r.status_code == 403
    db.refresh(target)
    assert verify_password("oldpass123", target.hashed_password)


def test_密碼過短回400(client, admin, target, auth_headers, db):
    r = client.post(f"/admin/users/{target.id}/password",
                    headers=auth_headers(admin), json={"new_password": "short"})
    assert r.status_code == 400
    db.refresh(target)
    assert verify_password("oldpass123", target.hashed_password)


def test_使用者不存在回404(client, admin, auth_headers):
    r = client.post("/admin/users/999999/password",
                    headers=auth_headers(admin), json={"new_password": "a123456789"})
    assert r.status_code == 404
