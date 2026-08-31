"""Task 1：reseller 相機範圍改由 camera_access 控管。

背景：平台改為非 admin 帳號不綁 camera_email，可見範圍全由 camera_access 表控管。
- get_allowed_camera_ids：僅 symotus_admin 回 None（不限制）；reseller 現在與 end_user
  一樣，回 camera_access 的 camera_id 清單。
- create_camera：配對成功（CB 回 200/201）且 role=="reseller" 時，若該 (camera_id,user)
  尚無 CameraAccess 就建一筆 full 權限 grant——不再看 used_admin_fallback（即使有
  camera_email/自有 token 也要建，否則自己剛配的相機在 get_allowed_camera_ids 收緊後
  就從清單中消失）。
"""
import asyncio
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, User, CameraAccess
from routers.cameras import get_allowed_camera_ids, create_camera, unbind_camera, get_live_frame_url
import routers.cameras as cameras_mod


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[User.__table__, CameraAccess.__table__])
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _user(db, id, role, camera_email=None):
    u = User(id=id, username=f"u{id}", email=f"u{id}@x.com", role=role, camera_email=camera_email)
    db.add(u)
    db.commit()
    return u


def _grant(db, camera_id, user_id, granted_by=None):
    db.add(CameraAccess(
        camera_id=camera_id, user_id=user_id, granted_by=granted_by or user_id,
        permission_level="full", invitation_id=0,
    ))
    db.commit()


# ── get_allowed_camera_ids ──────────────────────────────────────────────────

def test_symotus_admin_unrestricted(db):
    admin = _user(db, 1, "symotus_admin")
    assert get_allowed_camera_ids(admin, db) is None


def test_reseller_scoped_to_camera_access_grants(db):
    """reseller 現在與 end_user 一樣：只有 camera_access 有列的相機才在清單內。"""
    reseller = _user(db, 2, "reseller")
    _grant(db, camera_id=5, user_id=reseller.id)
    allowed = get_allowed_camera_ids(reseller, db)
    assert allowed == [5]
    assert 6 not in allowed  # 無 grant 的相機不在清單內


def test_reseller_without_any_grant_gets_empty_list(db):
    reseller = _user(db, 3, "reseller")
    assert get_allowed_camera_ids(reseller, db) == []


def test_end_user_behavior_unchanged(db):
    end_user = _user(db, 4, "end_user")
    _grant(db, camera_id=9, user_id=end_user.id, granted_by=2)
    allowed = get_allowed_camera_ids(end_user, db)
    assert allowed == [9]


# ── create_camera ────────────────────────────────────────────────────────────

class FakeRequest:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    async def body(self):
        return self._body


class FakeCBResponse:
    def __init__(self, status_code=201, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class FakeAsyncClient:
    """僅需回應 create_camera 內對 /api/cameras 的 POST。"""
    next_status = 201
    next_payload = {"id": 123}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, content=None, **kw):
        assert url.endswith("/api/cameras")
        return FakeCBResponse(FakeAsyncClient.next_status, FakeAsyncClient.next_payload)


@pytest.fixture(autouse=True)
def _patch_backend(monkeypatch):
    monkeypatch.setattr(cameras_mod.httpx, "AsyncClient", FakeAsyncClient)

    async def _fake_token(user):
        return "fake-cam-token"  # 有無 camera_email 都給 token，聚焦測試 grant 建立邏輯

    monkeypatch.setattr(cameras_mod, "get_camera_backend_token", _fake_token)
    FakeAsyncClient.next_status = 201
    FakeAsyncClient.next_payload = {"id": 123}
    yield


def test_create_camera_builds_grant_for_reseller(db):
    reseller = _user(db, 10, "reseller", camera_email=None)
    result = asyncio.run(create_camera(FakeRequest({"name": "cam"}), current_user=reseller, db=db))
    assert result.status_code == 201

    access = db.query(CameraAccess).filter(
        CameraAccess.camera_id == 123, CameraAccess.user_id == reseller.id,
    ).one()
    assert access.permission_level == "full"
    assert access.invitation_id == 0
    assert access.granted_by == reseller.id


def test_create_camera_builds_grant_even_with_camera_email(db):
    """核心行為改動：即便 reseller 本來就有 camera_email（用自有 token 配對），也要建 grant。"""
    reseller = _user(db, 11, "reseller", camera_email="reseller11@symotus.com")
    asyncio.run(create_camera(FakeRequest({"name": "cam"}), current_user=reseller, db=db))

    access = db.query(CameraAccess).filter(
        CameraAccess.camera_id == 123, CameraAccess.user_id == reseller.id,
    ).one()
    assert access.permission_level == "full"
    assert access.invitation_id == 0
    assert access.granted_by == reseller.id


def test_create_camera_does_not_duplicate_existing_grant(db):
    reseller = _user(db, 12, "reseller")
    asyncio.run(create_camera(FakeRequest({"name": "cam"}), current_user=reseller, db=db))
    asyncio.run(create_camera(FakeRequest({"name": "cam"}), current_user=reseller, db=db))

    rows = db.query(CameraAccess).filter(
        CameraAccess.camera_id == 123, CameraAccess.user_id == reseller.id,
    ).all()
    assert len(rows) == 1


def test_create_camera_failure_does_not_build_grant(db):
    reseller = _user(db, 13, "reseller")
    FakeAsyncClient.next_status = 400
    FakeAsyncClient.next_payload = {"detail": "boom"}
    asyncio.run(create_camera(FakeRequest({"name": "cam"}), current_user=reseller, db=db))

    rows = db.query(CameraAccess).filter(CameraAccess.user_id == reseller.id).all()
    assert rows == []


# ── 收緊後的 403 閘：reseller 對無 grant 的相機打受保護端點 ─────────────────────

def test_unbind_403_for_reseller_without_grant(db):
    reseller = _user(db, 20, "reseller")
    with pytest.raises(Exception) as e:
        asyncio.run(unbind_camera(camera_id=999, current_user=reseller, db=db))
    assert getattr(e.value, "status_code", None) == 403


def test_live_frame_url_403_for_reseller_without_grant(db):
    reseller = _user(db, 21, "reseller")
    with pytest.raises(Exception) as e:
        asyncio.run(get_live_frame_url(camera_id=999, current_user=reseller, db=db))
    assert getattr(e.value, "status_code", None) == 403


def test_live_frame_url_ok_for_reseller_with_grant(db):
    reseller = _user(db, 22, "reseller")
    _grant(db, camera_id=999, user_id=reseller.id)
    result = asyncio.run(get_live_frame_url(camera_id=999, current_user=reseller, db=db))
    assert "url" in result
