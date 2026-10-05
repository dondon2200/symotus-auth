"""工程模式（users.engineering_mode）：帳號層級的畫面旗標。

symotus_admin 在帳號管理切換；前端靠 JWT claim 與 /auth/me 決定要不要顯示
工程測試區塊（例：拍照星期 DAY0–DAY6）。它只影響畫面，不是權限。
"""
import pytest
from fastapi import FastAPI
from jose import jwt

from config import settings
from auth import create_access_token
from routers.admin import router as admin_router
from routers.auth import router as auth_router


@pytest.fixture()
def app():
    a = FastAPI()
    a.include_router(admin_router)
    a.include_router(auth_router)
    return a


@pytest.fixture()
def admin(make_user):
    return make_user("plat_admin", "plat_admin@test.com", password="adminpass1", role="symotus_admin")


@pytest.fixture()
def banny(make_user):
    return make_user("banny", "banny@test.com", password="bannypass1", role="end_user")


def _claims(token: str) -> dict:
    return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])


def test_新帳號預設關閉_token與me都是false(client, banny, auth_headers, db):
    assert banny.engineering_mode is False
    assert _claims(create_access_token(banny, db))["engineering_mode"] is False
    me = client.get("/auth/me", headers=auth_headers(banny)).json()
    assert me["engineering_mode"] is False


def test_admin可開啟_之後的token與me都是true(client, admin, banny, auth_headers, db):
    r = client.put(f"/admin/users/{banny.id}", headers=auth_headers(admin),
                   json={"engineering_mode": True})
    assert r.status_code == 200
    assert r.json()["engineering_mode"] is True
    db.refresh(banny)
    assert banny.engineering_mode is True
    assert _claims(create_access_token(banny, db))["engineering_mode"] is True
    assert client.get("/auth/me", headers=auth_headers(banny)).json()["engineering_mode"] is True


def test_admin可關閉(client, admin, banny, auth_headers, db):
    banny.engineering_mode = True
    db.commit()
    r = client.put(f"/admin/users/{banny.id}", headers=auth_headers(admin),
                   json={"engineering_mode": False})
    assert r.status_code == 200 and r.json()["engineering_mode"] is False
    db.refresh(banny)
    assert banny.engineering_mode is False


def test_非布林值回400且不改動(client, admin, banny, auth_headers, db):
    r = client.put(f"/admin/users/{banny.id}", headers=auth_headers(admin),
                   json={"engineering_mode": "yes"})
    assert r.status_code == 400
    db.refresh(banny)
    assert banny.engineering_mode is False


def test_一般使用者不能替自己開(client, banny, auth_headers, db):
    r = client.put(f"/admin/users/{banny.id}", headers=auth_headers(banny),
                   json={"engineering_mode": True})
    assert r.status_code == 403
    db.refresh(banny)
    assert banny.engineering_mode is False


def test_帳號清單帶出旗標(client, admin, banny, auth_headers, db):
    banny.engineering_mode = True
    db.commit()
    rows = client.get("/admin/users", headers=auth_headers(admin)).json()
    by_name = {u["username"]: u for u in rows}
    assert by_name["banny"]["engineering_mode"] is True
    assert by_name["plat_admin"]["engineering_mode"] is False


def test_只改其他欄位時不動旗標(client, admin, banny, auth_headers, db):
    banny.engineering_mode = True
    db.commit()
    r = client.put(f"/admin/users/{banny.id}", headers=auth_headers(admin),
                   json={"is_active": True})
    assert r.status_code == 200
    db.refresh(banny)
    assert banny.engineering_mode is True
