"""縮圖 admin fallback 逐 id 補查。

回歸來源：LINE 帳號（相機綁在 admin@timelapse.com 名下、僅靠 camera_access 授權）
用自己的 backend token 呼叫 Camera Backend /thumbnails/latest 會拿到 200 + `{}`，
舊邏輯只在「整個請求失敗或 body 為空 bytes」才走 admin fallback（b"{}" 為 truthy），
導致這類帳號的儀表板永遠沒有縮圖。
"""
import json
import asyncio

import pytest

import routers.cameras as cameras_mod
from routers.cameras import get_thumbnails


class FakeUser:
    def __init__(self, id=11, role="reseller", camera_email="line_x@symotus.com"):
        self.id = id
        self.role = role
        self.camera_email = camera_email


class FakeAccess:
    def __init__(self, camera_id):
        self.camera_id = camera_id


class FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._rows


class FakeDb:
    def __init__(self, granted_ids):
        self._rows = [FakeAccess(i) for i in granted_ids]

    def query(self, model):
        return FakeQuery(self._rows)


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.content = json.dumps(self._payload).encode()

    def json(self):
        return self._payload


class FakeAsyncClient:
    """依 Authorization header 分流：user token 與 admin token 回不同縮圖集。"""

    user_result: dict = {}
    admin_result: dict = {}
    calls: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, params=None):
        token = (headers or {}).get("Authorization", "")
        FakeAsyncClient.calls.append(("GET", token, dict(params or {})))
        if "admin-token" in token:
            return FakeResponse(200, FakeAsyncClient.admin_result)
        return FakeResponse(200, FakeAsyncClient.user_result)

    async def post(self, url, headers=None, json=None):
        FakeAsyncClient.calls.append(("POST", url, json))
        return FakeResponse(200, {"access_token": "admin-token"})


@pytest.fixture(autouse=True)
def patch_httpx(monkeypatch):
    monkeypatch.setattr(cameras_mod.httpx, "AsyncClient", FakeAsyncClient)
    FakeAsyncClient.calls = []
    FakeAsyncClient.user_result = {}
    FakeAsyncClient.admin_result = {}

    async def fake_token(user):
        return "user-token" if user and user.camera_email else ""

    monkeypatch.setattr(cameras_mod, "get_camera_backend_token", fake_token)
    yield


THUMB = {"path": "/x.jpg", "taken_at": "2026-08-11T00:00:00", "thumbnail_url": "/api/x"}


def test_line_account_empty_200_falls_back_to_admin_for_granted_ids():
    """自己 token 回 200 {}：有 grant 的 id 全部走 admin 補查。"""
    FakeAsyncClient.user_result = {}
    FakeAsyncClient.admin_result = {"37": THUMB, "7": THUMB}
    user = FakeUser(role="reseller")  # reseller → allowed_ids 不限制
    db = FakeDb(granted_ids=[37, 7])
    result = asyncio.run(get_thumbnails(ids="37,7", current_user=user, db=db))
    assert set(result.keys()) == {"37", "7"}


def test_partial_result_backfills_only_missing_ids():
    """自己 token 拿到一部分：只對缺漏且有 grant 的 id 補查，且不覆蓋原結果。"""
    mine = dict(THUMB, path="/mine.jpg")
    FakeAsyncClient.user_result = {"37": mine}
    FakeAsyncClient.admin_result = {"7": THUMB}
    user = FakeUser(role="reseller")
    db = FakeDb(granted_ids=[7])
    result = asyncio.run(get_thumbnails(ids="37,7", current_user=user, db=db))
    assert result["37"]["path"] == "/mine.jpg"
    assert result["7"] == THUMB
    fb_gets = [c for c in FakeAsyncClient.calls if c[0] == "GET" and "admin-token" in c[1]]
    assert fb_gets and fb_gets[0][2]["ids"] == "7"


def test_no_grant_no_fallback_for_non_admin():
    """缺漏但無 grant 的 id 不得經 admin fallback 洩漏（F-3）。"""
    FakeAsyncClient.user_result = {}
    FakeAsyncClient.admin_result = {"99": THUMB}
    user = FakeUser(role="reseller")
    db = FakeDb(granted_ids=[])
    result = asyncio.run(get_thumbnails(ids="99", current_user=user, db=db))
    assert result == {}
    assert not [c for c in FakeAsyncClient.calls if c[0] == "POST"]


def test_end_user_without_camera_email_uses_fallback():
    """無 camera_email 的 end_user：直接走 admin fallback，不會因 resp 未定義而 500。"""
    FakeAsyncClient.admin_result = {"7": THUMB}
    user = FakeUser(role="end_user", camera_email=None)
    db = FakeDb(granted_ids=[7])
    result = asyncio.run(get_thumbnails(ids="7", current_user=user, db=db))
    assert result == {"7": THUMB}
