"""Task 3：無自有 token 的 reseller（無 camera_email）打四個原本會硬壞的端點，
補 admin fallback / 改判定，一律以 camera_access 為界。

涵蓋：
- GET /cameras/nas/images：有 grant（含自我配對）才允許 admin fallback，否則 502。
- GET /cameras/nas/image：viewable grant（含自我配對）才允許 admin fallback。
- DELETE /cameras/{id}：非 admin 必須持有 CameraAccess，否則 403；有則可用 admin fallback token 刪除。
  symotus_admin 不受此限制。DELETE 是寫入操作，fallback 必須帶 admin@timelapse.com 在
  Camera Backend 的真實 camera_user_id（避開 camera-delete-backend-500 雷區——手造
  user_id=0 的 token 做寫入會 500）；讀取端點（nas/images）payload 的 user_id 維持 0 不變。
- POST /users/camera-access：除既有 full 列外，granted_by==self 的自我授權列（任何等級）
  也視為有資格轉授權。
"""
import asyncio

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, User, CameraAccess
import routers.cameras as cameras_mod
from routers.cameras import nas_images, nas_image, delete_camera
import routers.users as users_mod
from routers.users import grant_access_to_managed_user

# 在任何 autouse fixture monkeypatch 之前抓住真正的實作，供 payload 斷言測試使用
_REAL_GET_ADMIN_TOKEN = cameras_mod._get_admin_camera_token


# ── db fixture：獨立 in-memory sqlite，只建本測試需要的表 ─────────────────────

@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[User.__table__, CameraAccess.__table__])
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _user(db, id, role, camera_email=None, reseller_id=None, camera_user_id=None):
    u = User(id=id, username=f"u{id}", email=f"u{id}@x.com", role=role,
             camera_email=camera_email, reseller_id=reseller_id, camera_user_id=camera_user_id)
    db.add(u)
    db.commit()
    return u


def _grant(db, camera_id, user_id, granted_by=None, level="full"):
    g = CameraAccess(
        camera_id=camera_id, user_id=user_id, granted_by=granted_by or user_id,
        permission_level=level, invitation_id=0,
    )
    db.add(g)
    db.commit()
    return g


class FakeRequest:
    def __init__(self, params):
        self.query_params = params


class FakeResp:
    def __init__(self, status_code=200, json_data=None, content=b"", text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.content = content
        self.text = text or str(self._json)
        self.headers = {"content-type": "image/jpeg"}

    def json(self):
        return self._json


class FakeAsyncClient:
    """通用 fake httpx client：get/delete 狀態碼可由測試調整（class 變數）。"""
    get_status = 200
    delete_status = 200

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **k):
        return FakeResp(status_code=FakeAsyncClient.get_status, content=b"img-bytes")

    async def delete(self, *a, **k):
        return FakeResp(status_code=FakeAsyncClient.delete_status)


class CapturingAsyncClient(FakeAsyncClient):
    """繼承 FakeAsyncClient 的 get/delete；額外把 post() 送出的 json payload 記下來，
    供斷言 _get_admin_camera_token 實際打給 CB 的 /internal/auth/token body 內容。"""
    captured_payloads: list = []

    async def post(self, url, headers=None, json=None, **k):
        CapturingAsyncClient.captured_payloads.append(json)
        return FakeResp(status_code=200, json_data={"access_token": "admin-token"})


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    # reseller/end_user 一律無自有 camera token（新模型下無 camera_email）
    async def _no_own_token(user):
        return ""
    monkeypatch.setattr(cameras_mod, "get_camera_backend_token", _no_own_token)

    async def _admin_token(user_id: int = 0):
        return "admin-token"
    monkeypatch.setattr(cameras_mod, "_get_admin_camera_token", _admin_token)

    monkeypatch.setattr(cameras_mod.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(cameras_mod, "log_action", lambda *a, **k: None)
    monkeypatch.setattr(users_mod, "log_action", lambda *a, **k: None)
    FakeAsyncClient.get_status = 200
    FakeAsyncClient.delete_status = 200
    yield


# ── nas/images ────────────────────────────────────────────────────────────────

def test_nas_images_reseller_self_grant_ok(db, monkeypatch):
    reseller = _user(db, 30, "reseller", camera_email=None)
    _grant(db, camera_id=50, user_id=reseller.id, granted_by=reseller.id, level="full")

    async def _fake_backend(cam_token, camera_id, params):
        assert cam_token == "admin-token"
        return JSONResponse(status_code=200, content={"success": True, "data": {"files": []}})
    monkeypatch.setattr(cameras_mod, "list_nas_images_backend", _fake_backend)

    result = asyncio.run(nas_images(FakeRequest({"camera_id": "50"}), current_user=reseller, db=db))
    assert result.status_code == 200


def test_nas_images_reseller_without_grant_cannot_fallback(db):
    """無 grant → allow_fallback=False，不會借 admin token；斷言現行實際行為：502（拿不到 token）。"""
    reseller = _user(db, 31, "reseller")
    with pytest.raises(HTTPException) as e:
        asyncio.run(nas_images(FakeRequest({"camera_id": "999"}), current_user=reseller, db=db))
    assert e.value.status_code == 502


# ── nas/image ──────────────────────────────────────────────────────────────────

def test_nas_image_reseller_self_grant_ok(db):
    reseller = _user(db, 32, "reseller")
    _grant(db, camera_id=51, user_id=reseller.id, granted_by=reseller.id, level="full")

    req = FakeRequest({"path": "/homes/firmness/SN1/2026-08-31/a.jpg"})
    result = asyncio.run(nas_image(req, current_user=reseller, db=db))
    assert result.status_code == 200


def test_nas_image_reseller_without_any_access_502(db):
    reseller = _user(db, 33, "reseller")
    req = FakeRequest({"path": "/homes/firmness/SN1/2026-08-31/a.jpg"})
    with pytest.raises(HTTPException) as e:
        asyncio.run(nas_image(req, current_user=reseller, db=db))
    # 無任何 camera_access 列：accesses 為空，403 前置檢查 `accesses and not viewable` 不成立
    # （accesses 本身是空 list），流程繼續但 viewable 也是空 → 不觸發 admin fallback → 502
    assert e.value.status_code == 502


# ── DELETE /cameras/{id} ─────────────────────────────────────────────────────

def test_delete_camera_reseller_self_grant_ok(db):
    reseller = _user(db, 34, "reseller")
    _grant(db, camera_id=52, user_id=reseller.id, granted_by=reseller.id, level="full")

    result = asyncio.run(delete_camera(52, confirm=True, current_user=reseller, db=db))
    assert result["success"] is True

    remaining = db.query(CameraAccess).filter(CameraAccess.camera_id == 52).all()
    assert remaining == []  # 刪除後清掉殘留 grant


def test_delete_camera_reseller_without_grant_403(db):
    reseller = _user(db, 35, "reseller")
    with pytest.raises(HTTPException) as e:
        asyncio.run(delete_camera(999, confirm=True, current_user=reseller, db=db))
    assert e.value.status_code == 403


def test_delete_camera_admin_unaffected(db):
    """symotus_admin 不受「必須持有 grant」限制，維持既有行為。"""
    admin = _user(db, 1, "symotus_admin")
    result = asyncio.run(delete_camera(999, confirm=True, current_user=admin, db=db))
    assert result["success"] is True


def test_delete_camera_admin_fallback_uses_real_camera_user_id(db, monkeypatch):
    """camera-delete-backend-500 雷區：DELETE 的 admin fallback 是寫入操作，
    不可用假造 user_id=0 的 token；必須查 admin@timelapse.com 的真實 camera_user_id。"""
    reseller = _user(db, 40, "reseller")
    _grant(db, camera_id=60, user_id=reseller.id, granted_by=reseller.id, level="full")
    # 模擬 admin.py migrate_add_camera_user_id 已把 admin@timelapse.com 種好 camera_user_id=1
    _user(db, 2, "symotus_admin", camera_email="admin@timelapse.com", camera_user_id=1)

    monkeypatch.setattr(cameras_mod, "_get_admin_camera_token", _REAL_GET_ADMIN_TOKEN)
    monkeypatch.setattr(cameras_mod.httpx, "AsyncClient", CapturingAsyncClient)
    CapturingAsyncClient.captured_payloads = []

    result = asyncio.run(delete_camera(60, confirm=True, current_user=reseller, db=db))
    assert result["success"] is True
    assert CapturingAsyncClient.captured_payloads[-1]["user_id"] == 1


def test_delete_camera_admin_fallback_defaults_to_one_without_seeded_row(db, monkeypatch):
    """DB 裡找不到 admin@timelapse.com（或其 camera_user_id 未設）時 fallback 到 1，
    仍不可退回 0。"""
    reseller = _user(db, 41, "reseller")
    _grant(db, camera_id=62, user_id=reseller.id, granted_by=reseller.id, level="full")
    # 刻意不建立 admin@timelapse.com 這筆 User

    monkeypatch.setattr(cameras_mod, "_get_admin_camera_token", _REAL_GET_ADMIN_TOKEN)
    monkeypatch.setattr(cameras_mod.httpx, "AsyncClient", CapturingAsyncClient)
    CapturingAsyncClient.captured_payloads = []

    result = asyncio.run(delete_camera(62, confirm=True, current_user=reseller, db=db))
    assert result["success"] is True
    assert CapturingAsyncClient.captured_payloads[-1]["user_id"] == 1


def test_nas_images_admin_fallback_payload_user_id_unchanged(db, monkeypatch):
    """讀取端點行為零改動：nas/images 的 admin fallback payload user_id 仍是 0。"""
    reseller = _user(db, 42, "reseller")
    _grant(db, camera_id=61, user_id=reseller.id, granted_by=reseller.id, level="full")

    monkeypatch.setattr(cameras_mod, "_get_admin_camera_token", _REAL_GET_ADMIN_TOKEN)
    monkeypatch.setattr(cameras_mod.httpx, "AsyncClient", CapturingAsyncClient)
    CapturingAsyncClient.captured_payloads = []

    async def _fake_backend(cam_token, camera_id, params):
        return JSONResponse(status_code=200, content={"success": True, "data": {"files": []}})
    monkeypatch.setattr(cameras_mod, "list_nas_images_backend", _fake_backend)

    result = asyncio.run(nas_images(FakeRequest({"camera_id": "61"}), current_user=reseller, db=db))
    assert result.status_code == 200
    assert CapturingAsyncClient.captured_payloads[-1]["user_id"] == 0


# ── POST /users/camera-access（轉授權）────────────────────────────────────────

def test_grant_access_self_authorization_row_succeeds(db):
    """自我授權列（granted_by==self）不論等級都視為有資格轉授權（非僅 full）。"""
    reseller = _user(db, 36, "reseller")
    target = _user(db, 37, "end_user", reseller_id=reseller.id)
    _grant(db, camera_id=53, user_id=reseller.id, granted_by=reseller.id, level="stream_only")

    result = asyncio.run(grant_access_to_managed_user(
        body={"camera_id": 53, "user_id": target.id, "permission_level": "stream_only"},
        db=db, current_user=reseller,
    ))
    assert result["status"] == "created"

    new_access = db.query(CameraAccess).filter(
        CameraAccess.camera_id == 53, CameraAccess.user_id == target.id,
    ).one()
    assert new_access.granted_by == reseller.id


def test_grant_access_without_any_row_403(db):
    reseller = _user(db, 38, "reseller")
    target = _user(db, 39, "end_user", reseller_id=reseller.id)

    with pytest.raises(HTTPException) as e:
        asyncio.run(grant_access_to_managed_user(
            body={"camera_id": 54, "user_id": target.id, "permission_level": "stream_only"},
            db=db, current_user=reseller,
        ))
    assert e.value.status_code == 403
