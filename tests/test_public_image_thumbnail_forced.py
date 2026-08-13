"""C1／D3：公開連結 /image 代理永不回原檔——僅轉發白名單參數並強制 thumbnail=true。"""
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


def test_thumbnail_forced_and_extras_dropped():
    """帶 thumbnail=false 與其他參數：轉發時強制 thumbnail=true、其餘丟棄。"""
    req = FakeRequest({"path": "/homes/firmness/S/2026-08-13/a.jpg",
                       "thumbnail": "false", "evil": "1"})
    asyncio.run(get_public_nas_image(token="x", request=req, db=None))
    assert captured["params"] == {"path": "/homes/firmness/S/2026-08-13/a.jpg",
                                  "thumbnail": "true"}


def test_limit_whitelisted():
    req = FakeRequest({"path": "/p/a.jpg", "limit": "5", "thumbnail": "false"})
    asyncio.run(get_public_nas_image(token="x", request=req, db=None))
    assert captured["params"] == {"path": "/p/a.jpg", "thumbnail": "true", "limit": "5"}


def test_missing_path_rejected():
    req = FakeRequest({"thumbnail": "false"})
    with pytest.raises(HTTPException) as e:
        asyncio.run(get_public_nas_image(token="x", request=req, db=None))
    assert e.value.status_code == 400
