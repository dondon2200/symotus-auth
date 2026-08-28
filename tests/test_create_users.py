"""Task 5：後台建帳 API。
POST /admin/users（symotus_admin 限定）與 POST /users（reseller 限定）。"""
import pytest
from fastapi import FastAPI

from routers.admin import router as admin_router
from routers.users import users_router
from auth import verify_password


@pytest.fixture()
def app():
    a = FastAPI()
    a.include_router(admin_router)
    a.include_router(users_router)
    return a


@pytest.fixture()
def admin(make_user):
    return make_user("plat_admin", "plat_admin@test.com", password="adminpass1", role="symotus_admin")


@pytest.fixture()
def reseller(make_user):
    return make_user("plat_reseller", "plat_reseller@test.com", password="resellerpass1", role="reseller")


@pytest.fixture()
def end_user(make_user):
    return make_user("plat_enduser", "plat_enduser@test.com", password="enduserpass1", role="end_user")


def test_admin建reseller成功(client, admin, auth_headers, db):
    r = client.post("/admin/users", headers=auth_headers(admin), json={
        "username": "new_reseller", "email": "new_reseller@test.com",
        "password": "secretpw1", "role": "reseller",
    })
    assert r.status_code == 201
    body = r.json()
    assert body["role"] == "reseller"
    assert body["username"] == "new_reseller"

    from models import User
    user = db.query(User).filter(User.username == "new_reseller").first()
    assert user.created_by == admin.id
    assert verify_password("secretpw1", user.hashed_password)

    # 密碼可用於登入（透過 auth router 驗證 hash 正確）
    from auth import verify_password as vp
    assert vp("secretpw1", user.hashed_password)


def test_reseller建end_user成功且歸屬正確(client, reseller, auth_headers, db):
    r = client.post("/users", headers=auth_headers(reseller), json={
        "username": "new_enduser", "email": "new_enduser@test.com",
        "password": "secretpw1",
    })
    assert r.status_code == 201
    body = r.json()
    assert body["role"] == "end_user"
    assert body["reseller_id"] == reseller.id

    from models import User
    user = db.query(User).filter(User.username == "new_enduser").first()
    assert user.reseller_id == reseller.id
    assert user.created_by == reseller.id


def test_reseller_body夾帶role與reseller_id仍被忽略(client, reseller, auth_headers, db):
    other = None
    r = client.post("/users", headers=auth_headers(reseller), json={
        "username": "sneaky", "email": "sneaky@test.com",
        "password": "secretpw1", "role": "symotus_admin", "reseller_id": 9999,
    })
    assert r.status_code == 201
    body = r.json()
    assert body["role"] == "end_user"
    assert body["reseller_id"] == reseller.id


def test_enduser呼叫admin_users回403(client, end_user, auth_headers):
    r = client.post("/admin/users", headers=auth_headers(end_user), json={
        "username": "x", "email": "x@test.com", "password": "secretpw1", "role": "end_user",
    })
    assert r.status_code == 403


def test_enduser呼叫users回403(client, end_user, auth_headers):
    r = client.post("/users", headers=auth_headers(end_user), json={
        "username": "y", "email": "y@test.com", "password": "secretpw1",
    })
    assert r.status_code == 403


def test_admin建帳username重複回409(client, admin, auth_headers, reseller):
    r = client.post("/admin/users", headers=auth_headers(admin), json={
        "username": reseller.username, "email": "unique@test.com",
        "password": "secretpw1", "role": "end_user",
    })
    assert r.status_code == 409


def test_admin建帳email重複回409(client, admin, auth_headers, reseller):
    r = client.post("/admin/users", headers=auth_headers(admin), json={
        "username": "unique_username", "email": reseller.email,
        "password": "secretpw1", "role": "end_user",
    })
    assert r.status_code == 409


def test_reseller建帳username重複回409(client, reseller, auth_headers, admin):
    r = client.post("/users", headers=auth_headers(reseller), json={
        "username": admin.username, "email": "unique2@test.com", "password": "secretpw1",
    })
    assert r.status_code == 409


def test_password過短回422_admin端點(client, admin, auth_headers):
    r = client.post("/admin/users", headers=auth_headers(admin), json={
        "username": "shortpw", "email": "shortpw@test.com",
        "password": "short1", "role": "end_user",
    })
    assert r.status_code == 422


def test_password過短回422_users端點(client, reseller, auth_headers):
    r = client.post("/users", headers=auth_headers(reseller), json={
        "username": "shortpw2", "email": "shortpw2@test.com", "password": "short1",
    })
    assert r.status_code == 422


def test_admin建帳role非法值回422(client, admin, auth_headers):
    r = client.post("/admin/users", headers=auth_headers(admin), json={
        "username": "badrole", "email": "badrole@test.com",
        "password": "secretpw1", "role": "superuser",
    })
    assert r.status_code == 422
