"""公開分享列表端點：camera_id 強制取自邀請、僅放行分頁/日期參數。
緣由：公開頁縮時預覽/相簿原以 timesnap.serial 拼路徑打 nas/image（單數），
該欄位不存在且單數端點不支援目錄列表 → 404；改共用登入版列表核心。"""
import asyncio
import pytest

import routers.public_camera as pub
import routers.cameras as cams


class FakeInv:
    camera_id = 7


class FakeRequest:
    def __init__(self, params):
        self.query_params = params


@pytest.fixture(autouse=True)
def _patch_public_cam(monkeypatch):
    async def fake_get_public_cam(token, db):
        return FakeInv(), "granter-tok"
    monkeypatch.setattr(pub, "_get_public_cam", fake_get_public_cam)


def test_camera_id_forced_and_params_whitelisted(monkeypatch):
    captured = {}

    async def fake_list(cam_token, camera_id, params):
        captured.update(token=cam_token, camera_id=camera_id, params=params)
        return {"ok": True}

    monkeypatch.setattr(cams, "list_nas_images_backend", fake_list)
    req = FakeRequest({"limit": "8", "camera_id": "999", "evil": "1",
                       "start_time": "2026-08-14T00:00:00"})
    result = asyncio.run(pub.get_public_nas_images("tok", req, db=None))
    assert result == {"ok": True}
    assert captured["camera_id"] == "7"          # client 帶的 camera_id=999 被忽略
    assert captured["params"] == {"camera_id": "7", "limit": "8",
                                  "start_time": "2026-08-14T00:00:00"}  # evil 被丟棄
    assert captured["token"] == "granter-tok"
