"""公開連結 /image 代理：僅轉發白名單參數（path/thumbnail/limit），其餘丟棄。

thumbnail 由呼叫端決定、預設 true。2026-08-14 產品決議把公開頁的縮時播放與相簿
放大檢視改為比照登入版顯示原圖，故不再強制縮圖；白名單本身仍是防參數夾帶的閘門。
"""
import asyncio
import pytest
from fastapi import HTTPException

import routers.public_camera as public_mod
from routers.public_camera import get_public_nas_image


class FakeRequest:
    def __init__(self, params): self.query_params = params


class FakeResp:
    status_code = 200
    content = b"\xff\xd8jpg"
    headers = {"content-type": "image/jpeg"}


captured: dict = {}


class FakeAsyncClient:
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, url, **kw):
        captured["url"] = url
        captured["params"] = kw.get("params")
        return FakeResp()


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    captured.clear()
    async def fake_get_public_cam(token, db):
        return (object(), "tok")
    monkeypatch.setattr(public_mod, "_get_public_cam", fake_get_public_cam)
    monkeypatch.setattr(public_mod.httpx, "AsyncClient", FakeAsyncClient)


def test_extras_dropped_and_thumbnail_defaults_true():
    """未指定 thumbnail 時預設縮圖；夾帶的其他參數一律丟棄。"""
    req = FakeRequest({"path": "/homes/firmness/S/2026-08-13/a.jpg", "evil": "1"})
    asyncio.run(get_public_nas_image(token="x", request=req, db=None))
    assert captured["params"] == {"path": "/homes/firmness/S/2026-08-13/a.jpg",
                                  "thumbnail": "true"}


def test_original_requested_passes_through():
    """放大檢視/縮時播放要原圖：thumbnail=false 照實轉發。"""
    req = FakeRequest({"path": "/p/a.jpg", "thumbnail": "false", "evil": "1"})
    asyncio.run(get_public_nas_image(token="x", request=req, db=None))
    assert captured["params"] == {"path": "/p/a.jpg", "thumbnail": "false"}


def test_limit_whitelisted():
    req = FakeRequest({"path": "/p/a.jpg", "limit": "5"})
    asyncio.run(get_public_nas_image(token="x", request=req, db=None))
    assert captured["params"] == {"path": "/p/a.jpg", "thumbnail": "true", "limit": "5"}


def test_missing_path_rejected():
    req = FakeRequest({"thumbnail": "false"})
    with pytest.raises(HTTPException) as e:
        asyncio.run(get_public_nas_image(token="x", request=req, db=None))
    assert e.value.status_code == 400
