"""Task 4：timer-status / projects 靜默降級補 admin fallback，並依 allowed_ids 過濾；
DELETE project 補 project 歸屬檢查。

背景：這兩個 GET 端點原本「無自有 token 就直接回空」（reseller 沒有 camera_email 時
永遠看不到 timer 倒數、看不到 project 清單）。改為無自有 token 時退回 admin token，
但 admin token 能看到全部 CB 資料，因此回應前必須依 allowed_ids（camera_access）過濾，
否則會洩漏其他 reseller 的相機/project。

CB 的 `GET /api/projects`（ProjectResponse）不含相機清單，過濾/歸屬判斷改用
`GET /api/cameras` 每台相機的 `project_id` 欄位反查 project→camera 對應
（`_admin_camera_project_map`）。
"""
import asyncio
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, User, CameraAccess
import routers.cameras as cameras_mod
from routers.cameras import get_timer_status, list_projects, delete_project


# ── db fixture：獨立 in-memory sqlite ───────────────────────────────────────────

@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[User.__table__, CameraAccess.__table__])
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _user(db, id, role, camera_email=None, camera_user_id=None):
    u = User(id=id, username=f"u{id}", email=f"u{id}@x.com", role=role,
             camera_email=camera_email, camera_user_id=camera_user_id)
    db.add(u)
    db.commit()
    return u


def _grant(db, camera_id, user_id, level="full"):
    g = CameraAccess(camera_id=camera_id, user_id=user_id, granted_by=user_id,
                      permission_level=level, invitation_id=0)
    db.add(g)
    db.commit()
    return g


# ── fake httpx ───────────────────────────────────────────────────────────────

class FakeResp:
    """`json()` 每次回傳新複製的 dict——路由端會就地修改回應（過濾 cameras/projects），
    若直接回傳同一個物件參考，會像真實 httpx 一樣被覆寫，汙染其他測試用的共用 payload。"""
    def __init__(self, status_code=200, json_data=None):
        self._json = json_data if json_data is not None else {}
        self.status_code = status_code
        self.content = json.dumps(self._json).encode()
        self.text = str(self._json)

    def json(self):
        return json.loads(json.dumps(self._json))


TIMER_STATUS_PAYLOAD = {
    "server_time": "2026-08-31T00:00:00+08:00",
    "total": 2,
    "cameras": [
        {"camera_id": 7, "name": "cam7", "online_status": True, "has_timer": True, "current_state": "on"},
        {"camera_id": 99, "name": "cam99", "online_status": False, "has_timer": True, "current_state": "off"},
    ],
}

PROJECTS_PAYLOAD = {
    "projects": [
        {"id": 1, "name": "P1", "owner_id": 1},
        {"id": 2, "name": "P2", "owner_id": 1},
    ],
    "total": 2,
}

# 相機 7 屬 project 1，相機 99 屬 project 2（供 _admin_camera_project_map 反查）
CAMERAS_PAYLOAD = {
    "cameras": [
        {"id": 7, "project_id": 1},
        {"id": 99, "project_id": 2},
    ],
    "total": 2,
}


class FakeAsyncClient:
    """依 URL 分流回應；delete_status 供 DELETE 測試調整。"""
    delete_status = 200
    delete_calls: list = []
    post_payloads: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, params=None):
        if url.endswith("/api/cameras/timer-status"):
            return FakeResp(200, TIMER_STATUS_PAYLOAD)
        if url.endswith("/api/projects"):
            return FakeResp(200, PROJECTS_PAYLOAD)
        if url.endswith("/api/cameras"):
            return FakeResp(200, CAMERAS_PAYLOAD)
        return FakeResp(404, {})

    async def delete(self, url, headers=None):
        FakeAsyncClient.delete_calls.append(url)
        return FakeResp(FakeAsyncClient.delete_status, {"success": True})

    async def post(self, url, headers=None, json=None):
        # _get_admin_camera_token 用真正的實作（本檔未 monkeypatch），會經這裡打
        # /internal/auth/token；記下 payload 供斷言 project DELETE fallback 的 user_id。
        FakeAsyncClient.post_payloads.append(json)
        return FakeResp(200, {"access_token": "admin-token"})


@pytest.fixture(autouse=True)
def patch_httpx(monkeypatch):
    monkeypatch.setattr(cameras_mod.httpx, "AsyncClient", FakeAsyncClient)
    FakeAsyncClient.delete_status = 200
    FakeAsyncClient.delete_calls = []
    FakeAsyncClient.post_payloads = []

    async def fake_token(user):
        # 模擬「沒有自有 camera_email 就沒有自己的 CB token」
        return "user-token" if user and user.camera_email else ""
    monkeypatch.setattr(cameras_mod, "get_camera_backend_token", fake_token)
    yield


# ── timer-status ────────────────────────────────────────────────────────────

def test_timer_status_reseller_no_token_falls_back_and_filters(db):
    reseller = _user(db, 10, "reseller")
    _grant(db, camera_id=7, user_id=reseller.id)
    result = asyncio.run(get_timer_status(current_user=reseller, db=db))
    ids = {c["camera_id"] for c in result["cameras"]}
    assert ids == {7}
    assert result["total"] == 1


def test_timer_status_admin_not_filtered(db):
    admin = _user(db, 1, "symotus_admin")
    result = asyncio.run(get_timer_status(current_user=admin, db=db))
    ids = {c["camera_id"] for c in result["cameras"]}
    assert ids == {7, 99}
    assert result["total"] == 2


# ── projects list ────────────────────────────────────────────────────────────

def test_projects_list_reseller_no_token_falls_back_and_filters(db):
    reseller = _user(db, 11, "reseller")
    _grant(db, camera_id=7, user_id=reseller.id)  # 相機 7 屬 project 1
    result = asyncio.run(list_projects(current_user=reseller, db=db))
    ids = {p["id"] for p in result["projects"]}
    assert ids == {1}
    assert result["total"] == 1


def test_projects_list_admin_not_filtered(db):
    admin = _user(db, 2, "symotus_admin")
    result = asyncio.run(list_projects(current_user=admin, db=db))
    ids = {p["id"] for p in result["projects"]}
    assert ids == {1, 2}


# ── delete project ───────────────────────────────────────────────────────────

def test_delete_project_reseller_without_camera_in_project_403(db):
    reseller = _user(db, 12, "reseller")
    _grant(db, camera_id=7, user_id=reseller.id)  # 只有 project 1 的相機
    with pytest.raises(HTTPException) as e:
        asyncio.run(delete_project(2, current_user=reseller, db=db))  # 嘗試刪 project 2
    assert e.value.status_code == 403
    assert not FakeAsyncClient.delete_calls  # 未曾真的打到 CB


def test_delete_project_reseller_with_camera_in_project_succeeds(db):
    reseller = _user(db, 13, "reseller")
    _grant(db, camera_id=99, user_id=reseller.id)  # project 2 的相機
    result = asyncio.run(delete_project(2, current_user=reseller, db=db))
    assert result.status_code == 200
    assert FakeAsyncClient.delete_calls  # 有打到 CB


def test_delete_project_reseller_without_any_grant_403(db):
    reseller = _user(db, 14, "reseller")
    with pytest.raises(HTTPException) as e:
        asyncio.run(delete_project(1, current_user=reseller, db=db))
    assert e.value.status_code == 403


def test_delete_project_admin_unaffected(db):
    admin = _user(db, 3, "symotus_admin")
    result = asyncio.run(delete_project(1, current_user=admin, db=db))
    assert result.status_code == 200


def test_delete_project_fallback_uses_real_camera_user_id(db):
    """camera-delete-backend-500 雷區：project DELETE 的 admin fallback 是寫入操作，
    不可用假造 user_id=0 的 token；必須查 admin@timelapse.com 的真實 camera_user_id。"""
    reseller = _user(db, 15, "reseller")
    _grant(db, camera_id=99, user_id=reseller.id)  # project 2 的相機
    _user(db, 4, "symotus_admin", camera_email="admin@timelapse.com", camera_user_id=7)

    result = asyncio.run(delete_project(2, current_user=reseller, db=db))
    assert result.status_code == 200
    # 最後一次 /internal/auth/token 呼叫（實際打去做 DELETE 的那顆 token）帶真實 user_id，
    # 而非讀取歸屬用的 user_id=0
    assert FakeAsyncClient.post_payloads[-1]["user_id"] == 7


def test_delete_project_fallback_defaults_to_one_without_seeded_row(db):
    """DB 裡找不到 admin@timelapse.com（或其 camera_user_id 未設）時 fallback 到 1，
    仍不可退回 0。"""
    reseller = _user(db, 16, "reseller")
    _grant(db, camera_id=99, user_id=reseller.id)
    # 刻意不建立 admin@timelapse.com 這筆 User

    result = asyncio.run(delete_project(2, current_user=reseller, db=db))
    assert result.status_code == 200
    assert FakeAsyncClient.post_payloads[-1]["user_id"] == 1


# ── 邊角：allowed_ids=[]（reseller 無任何 grant）───────────────────────────────

def test_timer_status_reseller_no_grants_returns_empty(db):
    reseller = _user(db, 17, "reseller")  # 無任何 CameraAccess
    result = asyncio.run(get_timer_status(current_user=reseller, db=db))
    assert result["cameras"] == []
    assert result["total"] == 0


def test_projects_list_reseller_no_grants_returns_empty(db):
    reseller = _user(db, 18, "reseller")  # 無任何 CameraAccess
    result = asyncio.run(list_projects(current_user=reseller, db=db))
    assert result["projects"] == []
    assert result["total"] == 0
