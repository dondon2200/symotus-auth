"""Camera Backend token 5 分鐘快取。

回歸來源（2026-08-14 事故）：公開頁縮時預載一次抓上百張圖，每張都經圖片代理，
而代理每次都重新簽發 token → 等量的 internal/auth/token 呼叫塞爆連線池，
auth-service 被拖成 unhealthy、健康檢查與相簿列表一起 504。
"""
import asyncio
import pytest

import routers.cameras as cameras_mod
from routers.cameras import get_camera_backend_token


class FakeUser:
    def __init__(self, email="owner@x.com", role="reseller"):
        self.camera_email = email
        self.role = role


calls: list = []


class FakeResp:
    status_code = 200
    def json(self): return {"access_token": f"tok-{len(calls)}"}


class FakeClient:
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, **kw):
        calls.append(kw.get("json"))
        return FakeResp()


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    calls.clear()
    cameras_mod._cam_token_cache.clear()
    monkeypatch.setattr(cameras_mod.httpx, "AsyncClient", FakeClient)


def test_repeated_calls_mint_token_once():
    u = FakeUser()
    tokens = [asyncio.run(get_camera_backend_token(u)) for _ in range(5)]
    assert len(calls) == 1, "同帳號重複取用應共用快取，只簽發一次"
    assert len(set(tokens)) == 1


def test_cache_key_includes_role():
    """同 email 換角色要重簽，避免沿用舊權限的 token。"""
    asyncio.run(get_camera_backend_token(FakeUser(role="reseller")))
    asyncio.run(get_camera_backend_token(FakeUser(role="symotus_admin")))
    assert len(calls) == 2
    assert calls[0]["role"] != calls[1]["role"]


def test_different_accounts_not_shared():
    asyncio.run(get_camera_backend_token(FakeUser(email="a@x.com")))
    asyncio.run(get_camera_backend_token(FakeUser(email="b@x.com")))
    assert len(calls) == 2


def test_expiry_forces_refresh(monkeypatch):
    u = FakeUser()
    asyncio.run(get_camera_backend_token(u))
    # 讓快取過期
    key = (u.camera_email, cameras_mod.to_backend_role(u.role), 0)
    token, _exp = cameras_mod._cam_token_cache[key]
    cameras_mod._cam_token_cache[key] = (token, cameras_mod._time.monotonic() - 1)
    asyncio.run(get_camera_backend_token(u))
    assert len(calls) == 2


def test_no_camera_email_returns_empty_without_call():
    assert asyncio.run(get_camera_backend_token(FakeUser(email=None))) == ""
    assert calls == []
