"""D5：full 被分享者可刪相機（借 granter token）；等級不足仍 403。"""
import asyncio
import pytest
from fastapi import HTTPException

import routers.cameras as cameras_mod
from routers.cameras import delete_camera
from policies import invalidate_cache
from models import User, CameraAccess


class FakeUser:
    def __init__(self, id=11, role="end_user"):
        self.id = id
        self.role = role
        self.camera_email = None


class FakeQuery:
    def __init__(self, rows): self._rows = rows
    def filter(self, *a, **k): return self
    def first(self): return self._rows[0] if self._rows else None
    def all(self): return self._rows
    def delete(self, *a, **k): return len(self._rows)


class FakeDb:
    """query(CameraAccess) 回 grant、query(User) 回 granter。"""
    def __init__(self, grant, granter):
        self._grant, self._granter = grant, granter
    def query(self, model):
        if model is CameraAccess:
            return FakeQuery([self._grant] if self._grant else [])
        if model is User:
            return FakeQuery([self._granter] if self._granter else [])
        return FakeQuery([])
    def add(self, *a): pass
    def commit(self): pass


class _Grant:
    def __init__(self, level):
        self.camera_id = 7
        self.granted_by = 99
        self.permission_level = level


class FakeResp:
    status_code = 200
    text = ""


class FakeClient:
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def delete(self, *a, **k): return FakeResp()
    async def get(self, *a, **k): return FakeResp()  # _try_granter_token 的存取驗證


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    invalidate_cache()
    async def _token(user):
        return "granter-token" if getattr(user, "id", None) == 99 else ""
    monkeypatch.setattr(cameras_mod, "get_camera_backend_token", _token)
    monkeypatch.setattr(cameras_mod.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(cameras_mod, "log_action", lambda *a, **k: None)


def _granter():
    u = FakeUser(id=99, role="reseller")
    return u


def test_shared_full_can_delete():
    db = FakeDb(_Grant("full"), _granter())
    result = asyncio.run(delete_camera(7, confirm=True, current_user=FakeUser(), db=db))
    assert result is not None  # 未拋 403


def test_shared_photos_stream_cannot_delete():
    db = FakeDb(_Grant("photos_stream"), _granter())
    with pytest.raises(HTTPException) as e:
        asyncio.run(delete_camera(7, confirm=True, current_user=FakeUser(), db=db))
    assert e.value.status_code == 403
