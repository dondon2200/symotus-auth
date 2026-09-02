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
