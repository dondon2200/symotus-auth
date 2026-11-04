"""角色政策接線（cameras.py）：create_camera／create_project／delete_project／delete_camera 擁有者路徑。
預設下行為與改版前一致；翻轉 role_policies 列後行為改變。FakeDb：query(RolePolicy) 回假列。"""
import asyncio
import pytest
from fastapi import HTTPException
from starlette.requests import Request

import routers.cameras as cameras_mod
from routers.cameras import create_camera, create_project, delete_project, delete_camera
from role_policies import invalidate_role_cache
from models import RolePolicy, CameraAccess


class FakeUser:
    def __init__(self, id=11, role="end_user"):
        self.id, self.role, self.camera_email, self.camera_user_id = id, role, None, None


class _Row:
    def __init__(self, key, role, allowed):
        self.feature_key, self.role, self.allowed = key, role, allowed


class FakeQuery:
    def __init__(self, rows): self._rows = rows
    def filter(self, *a, **k): return self
    def first(self): return self._rows[0] if self._rows else None
    def all(self): return self._rows
    def delete(self, *a, **k): return len(self._rows)


class FakeDb:
    def __init__(self, role_rows=(), access=None):
        self.role_rows, self.access, self.added = list(role_rows), access, []
    def query(self, model):
        if model is RolePolicy: return FakeQuery(self.role_rows)
        if model is CameraAccess: return FakeQuery([self.access] if self.access else [])
        return FakeQuery([])
    def add(self, obj): self.added.append(obj)
    def commit(self): pass


class FakeResp:
    def __init__(self, status=201, body=None):
        self.status_code, self._body, self.text = status, body if body is not None else {"id": 7}, ""
        self.headers = {"content-type": "application/json"}
        self.content = b"{}"
    def json(self): return self._body


class FakeClient:
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, *a, **k): return FakeResp(201, {"id": 7})
    async def delete(self, *a, **k): return FakeResp(200, {})
    async def get(self, *a, **k): return FakeResp(200, {"projects": []})


def _request(body: bytes = b"{}") -> Request:
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    return Request({"type": "http", "method": "POST", "headers": [], "path": "/cameras", "query_string": b""}, receive)


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    invalidate_role_cache()
    async def _token(user): return "own-token"
    async def _admin_token(user_id=0): return "admin-token"
    async def _pmap(token): return {5: {7}}
    monkeypatch.setattr(cameras_mod, "get_camera_backend_token", _token)
    monkeypatch.setattr(cameras_mod, "_get_admin_camera_token", _admin_token)
    monkeypatch.setattr(cameras_mod, "_admin_camera_project_map", _pmap)
    monkeypatch.setattr(cameras_mod, "get_allowed_camera_ids", lambda user, db: [7])
    monkeypatch.setattr(cameras_mod, "assert_camera_unlocked", lambda *a, **k: None)
    monkeypatch.setattr(cameras_mod.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(cameras_mod, "log_action", lambda *a, **k: None)
    yield
    invalidate_role_cache()


def _403(coro):
    with pytest.raises(HTTPException) as e:
        asyncio.run(coro)
    assert e.value.status_code == 403


# ── camera.create ──────────────────────────────────────────────
def test_end_user_cannot_create_camera_by_default():
    _403(create_camera(_request(), current_user=FakeUser(role="end_user"), db=FakeDb()))


def test_reseller_creates_camera_and_gets_self_grant():
    db = FakeDb()
    resp = asyncio.run(create_camera(_request(), current_user=FakeUser(id=5, role="reseller"), db=db))
    assert resp.status_code == 201
    assert len(db.added) == 1
    g = db.added[0]
    assert (g.user_id, g.granted_by, g.permission_level, g.camera_id, g.invitation_id) == (5, 5, "full", 7, 0)


def test_end_user_can_create_camera_when_flipped_and_gets_self_grant():
    db = FakeDb(role_rows=[_Row("camera.create", "end_user", True)])
    resp = asyncio.run(create_camera(_request(), current_user=FakeUser(id=9, role="end_user"), db=db))
    assert resp.status_code == 201
    assert len(db.added) == 1 and db.added[0].user_id == 9


def test_reseller_blocked_when_camera_create_turned_off():
    _403(create_camera(_request(), current_user=FakeUser(role="reseller"),
                       db=FakeDb(role_rows=[_Row("camera.create", "reseller", False)])))


def test_admin_creates_camera_without_grant_regardless_of_rows():
    db = FakeDb(role_rows=[_Row("camera.create", "reseller", False)])
    resp = asyncio.run(create_camera(_request(), current_user=FakeUser(role="symotus_admin"), db=db))
    assert resp.status_code == 201 and db.added == []


# ── project.create / project.delete ───────────────────────────
def test_project_create_and_delete_follow_policy():
    _403(create_project(_request(), current_user=FakeUser(role="end_user"), db=FakeDb()))
    _403(delete_project(5, current_user=FakeUser(role="end_user"), db=FakeDb()))
    invalidate_role_cache()  # 60 秒快取：預設題已填入，翻轉前須清除
    flipped = FakeDb(role_rows=[_Row("project.create", "end_user", True), _Row("project.delete", "end_user", True)])
    assert asyncio.run(create_project(_request(), current_user=FakeUser(role="end_user"), db=flipped)) is not None
    assert asyncio.run(delete_project(5, current_user=FakeUser(role="end_user"), db=flipped)) is not None


# ── camera.delete.own ─────────────────────────────────────────
class _SelfGrant:
    def __init__(self, uid):
        self.camera_id, self.granted_by, self.permission_level, self.user_id = 7, uid, "full", uid


def test_end_user_owner_cannot_delete_by_default_but_can_when_flipped():
    _403(delete_camera(7, confirm=True, current_user=FakeUser(id=11), db=FakeDb(access=_SelfGrant(11))))
    invalidate_role_cache()  # 60 秒快取：預設題已填入，翻轉前須清除
    flipped = FakeDb(role_rows=[_Row("camera.delete.own", "end_user", True)], access=_SelfGrant(11))
    assert asyncio.run(delete_camera(7, confirm=True, current_user=FakeUser(id=11), db=flipped)) is not None


def test_reseller_owner_still_needs_grant():
    _403(delete_camera(7, confirm=True, current_user=FakeUser(id=3, role="reseller"), db=FakeDb(access=None)))
    ok = FakeDb(access=_SelfGrant(3))
    assert asyncio.run(delete_camera(7, confirm=True, current_user=FakeUser(id=3, role="reseller"), db=ok)) is not None
