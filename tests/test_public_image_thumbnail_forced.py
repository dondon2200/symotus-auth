"""公開連結 /image 代理：路徑必須屬於本邀請相機，且僅轉發白名單參數。

- thumbnail 由呼叫端決定、預設 true（2026-08-14 產品決議：相簿放大檢視顯示原圖）。
- path 須落在本機 serial 目錄下：分享者名下多台相機共用同一顆 granter token，
  未驗歸屬則任一公開連結可讀取該帳號其他相機的照片（2026-08-14 實測成立）。
"""
import asyncio
import pytest
from fastapi import HTTPException

import routers.public_camera as public_mod
from routers.public_camera import get_public_nas_image

SERIAL = "2025061200005"
OTHER_SERIAL = "2026032800025"


class FakeInv:
    camera_id = 7


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
    public_mod._serial_cache.clear()
    async def fake_get_public_cam(token, db):
        return (FakeInv(), "tok")
    async def fake_serial(camera_id, cam_token):
        return SERIAL
    monkeypatch.setattr(public_mod, "_get_public_cam", fake_get_public_cam)
    monkeypatch.setattr(public_mod, "_camera_serial", fake_serial)
    monkeypatch.setattr(public_mod.httpx, "AsyncClient", FakeAsyncClient)


def _own(name="a.jpg"):
    return f"/homes/firmness/{SERIAL}/2026-08-13/{name}"


def test_extras_dropped_and_thumbnail_defaults_true():
    """未指定 thumbnail 時預設縮圖；夾帶的其他參數一律丟棄。"""
    req = FakeRequest({"path": _own(), "evil": "1"})
    asyncio.run(get_public_nas_image(token="x", request=req, db=None))
    assert captured["params"] == {"path": _own(), "thumbnail": "true"}


def test_original_requested_passes_through():
    """相簿放大檢視要原圖：thumbnail=false 照實轉發。"""
    req = FakeRequest({"path": _own(), "thumbnail": "false", "evil": "1"})
    asyncio.run(get_public_nas_image(token="x", request=req, db=None))
    assert captured["params"] == {"path": _own(), "thumbnail": "false"}


def test_limit_whitelisted():
    req = FakeRequest({"path": _own(), "limit": "5"})
    asyncio.run(get_public_nas_image(token="x", request=req, db=None))
    assert captured["params"] == {"path": _own(), "thumbnail": "true", "limit": "5"}


def test_other_camera_path_forbidden():
    """核心回歸：同分享者名下其他相機的路徑必須 403（實測曾可讀取）。"""
    req = FakeRequest({"path": f"/homes/firmness/{OTHER_SERIAL}/2026-08-14/x.jpg"})
    with pytest.raises(HTTPException) as e:
        asyncio.run(get_public_nas_image(token="x", request=req, db=None))
    assert e.value.status_code == 403
    assert captured == {}, "不得把請求轉給 Camera Backend"


def test_prefix_confusion_forbidden():
    """serial 前綴相同的別台相機（如 2025061200005X）不得放行。"""
    req = FakeRequest({"path": f"/homes/firmness/{SERIAL}X/2026-08-14/x.jpg"})
    with pytest.raises(HTTPException) as e:
        asyncio.run(get_public_nas_image(token="x", request=req, db=None))
    assert e.value.status_code == 403


def test_missing_path_rejected():
    req = FakeRequest({"thumbnail": "false"})
    with pytest.raises(HTTPException) as e:
        asyncio.run(get_public_nas_image(token="x", request=req, db=None))
    assert e.value.status_code == 400
