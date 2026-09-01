"""symotus_admin 免 camera_email 自動取得 Camera Backend admin token。

緣由：admin 帳號要用 Camera Backend 功能，過去得手動在 DB 補
camera_email=admin@timelapse.com（見 memory「symotus_admin needs camera_email」）。
這次讓 get_camera_backend_token 對「role=symotus_admin 且無 camera_email」的帳號
自動換成 admin@timelapse.com 身分的 token，免手動改 DB；一般 reseller/end_user
無 camera_email 時行為不變（拿不到 token）。

user_id 用真實 camera_user_id（未設時 fallback 1），不可用 0——歷史事故
（camera-delete-backend-500）：這顆 token 也會被 DELETE 等寫入路徑共用，
假造 user_id=0 的 token 做寫入會被 Camera Backend 打 500。
"""
import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import routers.cameras as cameras_mod
from routers.cameras import get_camera_backend_token, list_cameras
from models import Base, User, CameraAccess


# ── get_camera_backend_token 單元測試（沿用 test_camera_token_cache.py 的 Fake 慣例）──

class FakeUser:
    def __init__(self, email=None, role="symotus_admin", camera_user_id=None):
        self.camera_email = email
        self.role = role
        self.camera_user_id = camera_user_id


calls: list = []


class FakeResp:
    status_code = 200

    def json(self):
        return {"access_token": f"tok-{len(calls)}"}


class FakeClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        calls.append(kw.get("json"))
        return FakeResp()


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    calls.clear()
    cameras_mod._cam_token_cache.clear()
    monkeypatch.setattr(cameras_mod.httpx, "AsyncClient", FakeClient)


def test_admin_without_camera_email_gets_token_default_uid_1():
    u = FakeUser(email=None, role="symotus_admin", camera_user_id=None)
    token = asyncio.run(get_camera_backend_token(u))
    assert token != ""
    assert calls[-1] == {"user_id": 1, "email": "admin@timelapse.com", "role": "admin"}


def test_admin_without_camera_email_uses_real_camera_user_id():
    u = FakeUser(email=None, role="symotus_admin", camera_user_id=7)
    token = asyncio.run(get_camera_backend_token(u))
    assert token != ""
    assert calls[-1] == {"user_id": 7, "email": "admin@timelapse.com", "role": "admin"}


@pytest.mark.parametrize("role", ["reseller", "end_user"])
def test_non_admin_without_camera_email_still_returns_empty(role):
    u = FakeUser(email=None, role=role, camera_user_id=None)
    token = asyncio.run(get_camera_backend_token(u))
    assert token == ""
    assert calls == []


def test_admin_with_camera_email_payload_unchanged():
    """有 camera_email 的既有帳號（含 admin）：payload 逐字節不變（沿用自己的 camera_email, user_id=0）。"""
    u = FakeUser(email="admin@timelapse.com", role="symotus_admin", camera_user_id=99)
    token = asyncio.run(get_camera_backend_token(u))
    assert token != ""
    assert calls[-1] == {"user_id": 0, "email": "admin@timelapse.com", "role": "admin"}


def test_cache_key_includes_uid_no_cross_identity_collision():
    """P0 回歸：TTL 內兩個無 camera_email 的 admin（不同 camera_user_id）連續換 token，
    快取鍵若只有 (email, role) 會互撞、其中一個會拿到對方 uid 的 token，寫入時重現
    user_id 誤帶的 500 雷區。此測試刻意不 clear cache，驗證兩者各自換發、各自命中。"""
    admin_a = FakeUser(email=None, role="symotus_admin", camera_user_id=7)
    admin_b = FakeUser(email=None, role="symotus_admin", camera_user_id=12)

    token_a = asyncio.run(get_camera_backend_token(admin_a))
    token_b = asyncio.run(get_camera_backend_token(admin_b))

    assert len(calls) == 2, "不同 uid 不可共用快取，應各自換一次 token"
    assert calls[0] == {"user_id": 7, "email": "admin@timelapse.com", "role": "admin"}
    assert calls[1] == {"user_id": 12, "email": "admin@timelapse.com", "role": "admin"}
    assert token_a != token_b

    # TTL 內重複呼叫（未 clear cache）應各自命中自己的快取，不互撞也不重新換發
    token_a_again = asyncio.run(get_camera_backend_token(admin_a))
    token_b_again = asyncio.run(get_camera_backend_token(admin_b))
    assert len(calls) == 2, "TTL 內重複呼叫應命中快取，不重新換發"
    assert token_a_again == token_a
    assert token_b_again == token_b


def test_cache_key_distinguishes_legacy_email_bound_admin_from_fallback():
    """同 email(admin@timelapse.com)、同 role(admin)，但 uid 不同（舊 email 綁定路徑
    uid=0 vs 免綁定 fallback 預設 uid=1）不可共用快取，否則寫入時彼此互相冒用 uid。"""
    legacy_admin = FakeUser(email="admin@timelapse.com", role="symotus_admin", camera_user_id=None)
    fallback_admin = FakeUser(email=None, role="symotus_admin", camera_user_id=None)

    token_legacy = asyncio.run(get_camera_backend_token(legacy_admin))
    token_fallback = asyncio.run(get_camera_backend_token(fallback_admin))

    assert len(calls) == 2
    assert calls[0] == {"user_id": 0, "email": "admin@timelapse.com", "role": "admin"}
    assert calls[1] == {"user_id": 1, "email": "admin@timelapse.com", "role": "admin"}
    assert token_legacy != token_fallback


# ── 整合測試：GET /cameras（無 camera_email 的 admin）──────────────────────────

class FakeListClient(FakeClient):
    """除了 token 換發用的 post，還要處理 list_cameras 內對 /api/cameras 的 get。"""

    async def get(self, url, **kw):
        class R:
            status_code = 200

            def json(self):
                return {"cameras": [{"id": 1, "name": "cam1"}, {"id": 2, "name": "cam2"}]}

        return R()


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[User.__table__, CameraAccess.__table__])
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def test_list_cameras_admin_without_camera_email_returns_unfiltered(db, monkeypatch):
    admin = User(id=1, username="admin1", email="admin1@x.com", role="symotus_admin", camera_email=None)
    db.add(admin)
    db.commit()

    monkeypatch.setattr(cameras_mod.httpx, "AsyncClient", FakeListClient)

    result = asyncio.run(list_cameras(current_user=admin, db=db))
    assert result["total"] == 2
    assert len(result["cameras"]) == 2
