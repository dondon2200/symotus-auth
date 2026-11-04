"""角色權限 API：admin 讀寫、驗證、reset、稽核；任何登入者讀 /role-policies/me。"""
import pytest
from fastapi import FastAPI

from models import AuditLog, RolePolicy
from role_policies import ROLE_FEATURE_DEFAULTS, ROLES, invalidate_role_cache
from routers.role_policy_api import router as rp_router


@pytest.fixture()
def app():
    a = FastAPI()
    a.include_router(rp_router)
    return a


@pytest.fixture(autouse=True)
def _cache():
    invalidate_role_cache()
    yield
    invalidate_role_cache()


@pytest.fixture()
def admin(make_user):
    return make_user("plat_admin", "plat_admin@test.com", password="pw", role="symotus_admin")


@pytest.fixture()
def reseller(make_user):
    return make_user("r1", "r1@test.com", password="pw", role="reseller")


@pytest.fixture()
def end_user(make_user):
    return make_user("e1", "e1@test.com", password="pw", role="end_user")


def test_list_returns_catalogue_with_defaults_when_table_empty(client, admin, auth_headers):
    r = client.get("/admin/role-policies", headers=auth_headers(admin))
    assert r.status_code == 200
    body = r.json()
    assert [row["feature_key"] for row in body] == [k for k, *_ in ROLE_FEATURE_DEFAULTS]
    cam = next(row for row in body if row["feature_key"] == "camera.create")
    assert cam["roles"] == {"reseller": {"allowed": True, "is_default": True},
                           "end_user": {"allowed": False, "is_default": True}}
    assert cam["group"] == "相機與專案" and cam["description"] == "新增／配對相機"


def test_non_admin_cannot_use_admin_endpoints(client, reseller, auth_headers):
    h = auth_headers(reseller)
    assert client.get("/admin/role-policies", headers=h).status_code == 403
    assert client.put("/admin/role-policies/camera.create/end_user", headers=h, json={"allowed": True}).status_code == 403
    assert client.post("/admin/role-policies/reset", headers=h).status_code == 403


def test_put_flips_me_and_writes_audit(client, admin, end_user, auth_headers, db):
    assert client.get("/role-policies/me", headers=auth_headers(end_user)).json() == {"role": "end_user", "allowed": []}
    r = client.put("/admin/role-policies/camera.create/end_user", headers=auth_headers(admin), json={"allowed": True})
    assert r.status_code == 200
    assert r.json() == {"feature_key": "camera.create", "role": "end_user", "allowed": True, "is_default": False}
    assert client.get("/role-policies/me", headers=auth_headers(end_user)).json()["allowed"] == ["camera.create"]
    log = db.query(AuditLog).filter(AuditLog.action == "update_role_policy").one()
    assert log.detail == "camera.create/end_user=True"
    listed = client.get("/admin/role-policies", headers=auth_headers(admin)).json()
    cam = next(row for row in listed if row["feature_key"] == "camera.create")
    assert cam["roles"]["end_user"] == {"allowed": True, "is_default": False}


def test_put_validation(client, admin, auth_headers):
    h = auth_headers(admin)
    assert client.put("/admin/role-policies/not.a.key/end_user", headers=h, json={"allowed": True}).status_code == 404
    assert client.put("/admin/role-policies/camera.create/symotus_admin", headers=h, json={"allowed": False}).status_code == 400
    assert client.put("/admin/role-policies/camera.create/end_user", headers=h, json={"allowed": "yes"}).status_code == 400


def test_reset_restores_defaults_and_audits(client, admin, end_user, auth_headers, db):
    h = auth_headers(admin)
    client.put("/admin/role-policies/camera.create/end_user", headers=h, json={"allowed": True})
    client.put("/admin/role-policies/camera.create/reseller", headers=h, json={"allowed": False})
    assert client.post("/admin/role-policies/reset", headers=h).status_code == 200
    assert db.query(RolePolicy).count() == len(ROLE_FEATURE_DEFAULTS) * len(ROLES)
    assert client.get("/role-policies/me", headers=auth_headers(end_user)).json()["allowed"] == []
    assert db.query(AuditLog).filter(AuditLog.action == "reset_role_policies").count() == 1


def test_me_for_each_role(client, admin, reseller, end_user, auth_headers):
    all_keys = [k for k, *_ in ROLE_FEATURE_DEFAULTS]
    assert client.get("/role-policies/me", headers=auth_headers(admin)).json()["allowed"] == all_keys
    reseller_keys = [k for k, _g, _d, d in ROLE_FEATURE_DEFAULTS if d["reseller"]]
    assert client.get("/role-policies/me", headers=auth_headers(reseller)).json()["allowed"] == reseller_keys
    assert client.get("/role-policies/me", headers=auth_headers(end_user)).json()["allowed"] == []
    assert client.get("/role-policies/me").status_code in (401, 403)
