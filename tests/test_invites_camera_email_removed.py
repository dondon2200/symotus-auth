"""Task 5：建帳/邀請流程移除 camera_email。

InviteToken.camera_ids 是 postgresql ARRAY(Integer)，sqlite 建表會直接炸
（既有測試套件裡完全沒有 invites 相關測試，就是因為這個限制）。這裡註冊一個
只在 sqlite dialect 生效、僅供測試用的 DDL 編譯覆寫（camera_ids 全程留 None，
不觸發 ARRAY 的 bind processor），讓 invite_tokens 表能在 sqlite 建起來，藉此
對 routers/invites.py 與 routers/auth.py 的 register() 邀請路徑做真正的整合測試。
不影響任何正式程式碼——production 仍在 PostgreSQL 上跑，ARRAY 照舊。
"""
import asyncio

import pytest
from sqlalchemy import ARRAY, create_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from datetime import datetime, timedelta

from models import Base, User, InviteToken, RefreshToken, CameraAccess
import routers.auth as auth_mod
from routers.auth import register, UserCreateInternal
from routers.invites import create_invite
from schemas import InviteCreate


@compiles(ARRAY, "sqlite")
def _compile_array_as_json_for_sqlite(element, compiler, **kw):  # pragma: no cover - DDL only
    return "JSON"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[
        User.__table__, InviteToken.__table__, RefreshToken.__table__, CameraAccess.__table__,
    ])
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


@pytest.fixture()
def reseller(db):
    u = User(id=1, username="r1", email="r1@x.com", role="reseller")
    db.add(u)
    db.commit()
    return u


@pytest.fixture()
def platform_admin(db):
    u = User(id=2, username="admin1", email="admin1@x.com", role="symotus_admin")
    db.add(u)
    db.commit()
    return u


class FakeRequest:
    """register() 需要 Request 給 _rate_limit 讀 headers/client；is_internal 路徑不會用到。"""
    def __init__(self):
        self.headers = {}
        self.client = None


@pytest.fixture(autouse=True)
def _no_network_camera_token(monkeypatch):
    """register() 會 await get_camera_token() 向 Camera Backend 換 token，
    測試環境沒有真實後端，直接 stub 掉避免打外部網路。"""
    async def _fake(user_id, email, role, camera_email=None):
        return {}
    monkeypatch.setattr(auth_mod, "get_camera_token", _fake)


# ── POST /invites：camera_email/camera_user_id 被忽略 ──────────────────────

def test_invite建立夾帶camera_email被忽略(db, platform_admin):
    """InviteCreate 已移除 camera_email/camera_user_id 欄位，body 夾帶時
    Pydantic 靜默忽略；建出的 invite 兩欄位皆為 None。"""
    body = InviteCreate(
        intended_role="reseller",
        expires_hours=48,
        **{"camera_email": "hacker@evil.com", "camera_user_id": 999},
    )
    result = create_invite(body, db=db, current_user=platform_admin)
    assert result["camera_email"] is None

    invite = db.query(InviteToken).filter(InviteToken.id == result["id"]).first()
    assert invite.camera_email is None
    assert invite.camera_user_id is None


def test_invite_schema不再接受camera_email欄位():
    body = InviteCreate(intended_role="end_user", **{"camera_email": "x@y.com", "camera_user_id": 1})
    assert not hasattr(body, "camera_email")
    assert not hasattr(body, "camera_user_id")


# ── register()：接受邀請後帳號 camera_email 一律 None ───────────────────────

def _make_invite(db, reseller_id, intended_role="end_user", camera_email=None, camera_user_id=None):
    invite = InviteToken(
        reseller_id=reseller_id,
        email=None,
        intended_role=intended_role,
        camera_email=camera_email,      # 模擬舊資料殘留（router 已不再寫入，但欄位仍在）
        camera_user_id=camera_user_id,
        expires_at=datetime.utcnow() + timedelta(hours=1),
    )
    db.add(invite)
    db.commit()
    db.refresh(invite)
    return invite


def test_register_邀請註冊不繼承invite的camera_email(db, reseller):
    """即便 invite row 上（舊資料殘留）還留著 camera_email/camera_user_id，
    register() 也不應該把它們繼承給新帳號——公開邀請路徑一律 None。"""
    invite = _make_invite(db, reseller.id, camera_email="legacy@symotus.com", camera_user_id=42)

    body = UserCreateInternal(
        username="invitee1", email="invitee1@test.com", password="pw12345678",
        invite_token=invite.token,
    )
    result = asyncio.run(register(body, FakeRequest(), db=db, x_service_key=None, authorization=None))
    assert result.access_token

    user = db.query(User).filter(User.username == "invitee1").first()
    assert user is not None
    assert user.camera_email is None
    assert user.camera_user_id is None


def test_register_internal路徑body夾帶camera_email也被忽略(db, platform_admin, monkeypatch):
    """internal 建帳路徑（x-service-key）：UserCreateInternal 已移除
    camera_email 欄位，body 夾帶時 Pydantic 忽略，建出的帳號 camera_email 為 None。
    用 x-service-key 而非 symotus_admin JWT 觸發 internal 路徑——register() 目前
    判斷 JWT 路徑時對 TokenPayload（pydantic model）呼叫 .get("role")，與這次
    camera_email 清理無關的既有問題，不在本次改動範圍內，因此改用 service-key 路徑。"""
    monkeypatch.setattr(auth_mod, "CAMERA_SERVICE_KEY", "test-service-key")

    body = UserCreateInternal(
        username="internal_user", email="internal_user@test.com", password="pw12345678",
        role="end_user", **{"camera_email": "hacker@evil.com"},
    )
    result = asyncio.run(register(body, FakeRequest(), db=db, x_service_key="test-service-key",
                                   authorization=None))
    assert result.access_token

    user = db.query(User).filter(User.username == "internal_user").first()
    assert user is not None
    assert user.camera_email is None
