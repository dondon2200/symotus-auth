"""送出前分析（precheck）：權限前綴歸類，與輪詢代理的 token 退路。"""
import asyncio
import pytest
from fastapi import HTTPException

import routers.cameras as cameras_mod
from routers.cameras import get_timelapse_precheck
from policies import feature_for_write


class FakeUser:
    id = 11
    role = "end_user"
    camera_email = None


class FakeResp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload
        self.content = b"{}" if payload is not None else b""

    def json(self):
        return self._payload


class FakeClient:
    """httpx.AsyncClient 替身：依 token 回不同結果，並記錄呼叫順序。"""
    calls: list = []
    by_token: dict = {}

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def get(self, url, headers=None):
        tok = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
        FakeClient.calls.append((url, tok))
        return FakeClient.by_token.get(tok, FakeResp(404, {"detail": "Not Found"}))


@pytest.fixture(autouse=True)
def _fake_http(monkeypatch):
    FakeClient.calls = []
    FakeClient.by_token = {}
    monkeypatch.setattr(cameras_mod.httpx, "AsyncClient", FakeClient)
    async def _own(user): return "user-tok"
    async def _admin(user_id=0): return "admin-tok"
    monkeypatch.setattr(cameras_mod, "get_camera_backend_token", _own)
    monkeypatch.setattr(cameras_mod, "_get_admin_camera_token", _admin)


PID = "1a3b9dc4-85d0-4067-a47e-fe28e13779df"


def test_precheck_is_timelapse_feature_not_settings():
    """被分享者（photos_stream）能產縮時就能先分析；若落到 camera.settings 會被 403。"""
    assert feature_for_write("timelapse-precheck") == "timelapse.create"
    assert feature_for_write("timelapse-jobs") == "timelapse.create"


def test_poll_with_own_token():
    FakeClient.by_token["user-tok"] = FakeResp(200, {"status": "completed", "result": {"affected_days": 2}})
    r = asyncio.run(get_timelapse_precheck(PID, current_user=FakeUser()))
    assert r.status_code == 200
    assert b'"affected_days":2' in r.body or b'"affected_days": 2' in r.body
    assert [t for _, t in FakeClient.calls] == ["user-tok"]
    assert FakeClient.calls[0][0].endswith(f"/api/timelapse-prechecks/{PID}")


def test_poll_falls_back_to_admin_when_own_token_rejected():
    """送出時若是用 admin token 送的，CB 用使用者 token 查會 404：要退 admin 再試。"""
    FakeClient.by_token["admin-tok"] = FakeResp(200, {"status": "running"})
    r = asyncio.run(get_timelapse_precheck(PID, current_user=FakeUser()))
    assert r.status_code == 200
    assert [t for _, t in FakeClient.calls] == ["user-tok", "admin-tok"]


def test_poll_passes_through_backend_status_after_fallback():
    """兩顆 token 都查不到就把 CB 的 404 原樣回去，前端靜默收掉。"""
    r = asyncio.run(get_timelapse_precheck(PID, current_user=FakeUser()))
    assert r.status_code == 404


def test_bad_id_rejected_before_any_request():
    with pytest.raises(HTTPException) as e:
        asyncio.run(get_timelapse_precheck("../etc", current_user=FakeUser()))
    assert e.value.status_code == 422
    assert FakeClient.calls == []
