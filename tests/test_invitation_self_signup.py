"""相機分享自助建帳（spec 2026-09-02 S1-S5）。sqlite in-memory 只建需要的表。"""
import asyncio
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import (
    Base, User, CameraInvitation, CameraAccess, AuditLog, FeaturePolicy, RefreshToken,
)
from policies import invalidate_cache


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[
        User.__table__, CameraInvitation.__table__, CameraAccess.__table__,
        AuditLog.__table__, FeaturePolicy.__table__, RefreshToken.__table__,
    ])
    invalidate_cache()   # 政策表為空 → level_allows 走 FEATURE_DEFAULTS fallback
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    invalidate_cache()


@pytest.fixture()
def reseller(db):
    u = User(id=1, username="rs", email="rs@x.com", role="reseller")
    db.add(u); db.commit()
    return u


def test_new_columns_have_expected_defaults(db, reseller):
    inv = CameraInvitation(token="tok1", inviter_id=reseller.id, camera_id=7,
                           permission_level="photos_stream")
    db.add(inv); db.commit(); db.refresh(inv)
    assert inv.invitee_email is None
    assert inv.signup_limit is None      # NULL 由 _signup_limit() 視為 10
    assert inv.signup_count == 0


from routers.invitations import CreateInvitationBody, create_invitation


def _create(db, inviter, level, **kw):
    body = CreateInvitationBody(camera_id=7, camera_name="cam7",
                                permission_level=level, **kw)
    return create_invitation(body, db=db, current_user=inviter)


def test_full_requires_invitee_email(db, reseller):
    with pytest.raises(HTTPException) as e:
        _create(db, reseller, "full")
    assert e.value.status_code == 400
    assert "Email" in e.value.detail


def test_full_with_email_sets_limit_one(db, reseller):
    out = _create(db, reseller, "full", invitee_email="Bob@Example.com")
    inv = db.query(CameraInvitation).filter_by(token=out["token"]).first()
    assert inv.invitee_email == "bob@example.com"   # 正規化為小寫
    assert inv.signup_limit == 1
    assert out["invitee_email"] == "bob@example.com"


def test_open_link_leaves_limit_null(db, reseller):
    out = _create(db, reseller, "photos_stream")
    inv = db.query(CameraInvitation).filter_by(token=out["token"]).first()
    assert inv.invitee_email is None
    assert inv.signup_limit is None


def test_public_link_rejects_invitee_email(db, reseller):
    with pytest.raises(HTTPException) as e:
        _create(db, reseller, "stream_only", is_public=True, invitee_email="a@b.com")
    assert e.value.status_code == 400


def test_dedupe_is_per_invitee_email(db, reseller):
    """D1 修訂：同相機同模式、不同對象各自成鏈，同對象重複建立回收舊連結。"""
    a = _create(db, reseller, "full", invitee_email="a@x.com")
    b = _create(db, reseller, "full", invitee_email="b@x.com")
    assert a["token"] != b["token"]
    again = _create(db, reseller, "full", invitee_email="A@X.com")
    assert again["token"] == a["token"] and again.get("reused") is True


from routers.invitations import _signup_limit, SIGNUP_LIMIT_DEFAULT


def test_signup_limit_default_fallback():
    """_signup_limit 將 None 視為 SIGNUP_LIMIT_DEFAULT（10）；有值則直接回傳。"""
    inv_none = CameraInvitation(token="tok1", inviter_id=1, camera_id=7,
                                permission_level="photos_stream", signup_limit=None)
    assert _signup_limit(inv_none) == SIGNUP_LIMIT_DEFAULT
    assert _signup_limit(inv_none) == 10

    inv_value = CameraInvitation(token="tok2", inviter_id=1, camera_id=7,
                                 permission_level="photos_stream", signup_limit=5)
    assert _signup_limit(inv_value) == 5


def test_dedupe_reuses_when_no_invitee_email(db, reseller):
    """驗證 SQLAlchemy 將 invitee_email == None 轉譯為 IS NULL（非 = NULL）。

    若此轉譯出錯（例如變成 = NULL），未指定對象的連結每次都會新建而不會 dedupe。
    此測試確保：(1) 同 reseller、同相機、同模式、無指定對象，第二次回收舊連結；
    (2) 回傳的 token 相同；(3) reused 為 True；(4) DB 裡只有一筆該相機該模式的連結。
    """
    # 第一次建立無指定對象的連結
    first = _create(db, reseller, "photos_stream")
    first_token = first["token"]
    assert first.get("reused") != True  # 首次應該是新建

    # 驗證 DB 中有一筆
    count_1 = db.query(CameraInvitation).filter(
        CameraInvitation.inviter_id == reseller.id,
        CameraInvitation.camera_id == 7,
        CameraInvitation.permission_level == "photos_stream",
        CameraInvitation.invitee_email.is_(None),
    ).count()
    assert count_1 == 1

    # 第二次建立相同條件的連結，應該回收第一次的
    second = _create(db, reseller, "photos_stream")
    assert second["token"] == first_token
    assert second.get("reused") is True

    # 驗證 DB 仍只有一筆（沒有新建）
    count_2 = db.query(CameraInvitation).filter(
        CameraInvitation.inviter_id == reseller.id,
        CameraInvitation.camera_id == 7,
        CameraInvitation.permission_level == "photos_stream",
        CameraInvitation.invitee_email.is_(None),
    ).count()
    assert count_2 == 1
