"""D3：nas/image 原圖請求對 stream_only 被分享者 403（縮圖仍可，供相簿瀏覽）。"""
import asyncio
import pytest
from fastapi import HTTPException

import routers.cameras as cameras_mod
from routers.cameras import nas_image
from policies import invalidate_cache


class FakeUser:
    id = 11
    role = "end_user"
    camera_email = None


class FakeAccess:
    def __init__(self):
        self.camera_id = 7
        self.granted_by = 99      # 別人分享給我
        self.permission_level = "stream_only"


class FakeQuery:
    def __init__(self, rows): self._rows = rows
    def filter(self, *a, **k): return self
    def all(self): return self._rows
    def first(self): return self._rows[0] if self._rows else None


class FakeDb:
    def query(self, model):
        if model.__name__ == "CameraAccess":
            return FakeQuery([FakeAccess()])
        return FakeQuery([])


class FakeRequest:
    def __init__(self, params): self.query_params = params


@pytest.fixture(autouse=True)
def _no_own_token(monkeypatch):
    invalidate_cache()
    async def _none(user): return ""   # 被分享者沒有自己的 backend token
    monkeypatch.setattr(cameras_mod, "get_camera_backend_token", _none)
    async def _no_admin(): return ""   # admin fallback 也拿不到（避免測試打真的網路）
    monkeypatch.setattr(cameras_mod, "_get_admin_camera_token", _no_admin)


def test_original_image_forbidden_for_stream_only():
    req = FakeRequest({"path": "/homes/firmness/SN1/2026-08-13/a.jpg"})
    with pytest.raises(HTTPException) as e:
        asyncio.run(nas_image(req, current_user=FakeUser(), db=FakeDb()))
    assert e.value.status_code == 403
    assert "photos.download" in e.value.detail


def test_thumbnail_still_viewable_for_stream_only(monkeypatch):
    """縮圖走 photos.view（stream_only 可）：不該在等級檢查就 403。
    granter token 也拿不到時會落到 502，這正好證明通過了等級閘門。"""
    req = FakeRequest({"path": "/homes/firmness/SN1/2026-08-13/a.jpg", "thumbnail": "true"})
    with pytest.raises(HTTPException) as e:
        asyncio.run(nas_image(req, current_user=FakeUser(), db=FakeDb()))
    assert e.value.status_code == 502
