"""三模式邀請邏輯（spec D1/D2＋定案②）。sqlite in-memory 只建需要的表。"""
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, User, CameraInvitation, CameraAccess, AuditLog
from routers.invitations import (
    CreateInvitationBody, create_invitation, accept_invitation, cancel_invitation,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[
        User.__table__, CameraInvitation.__table__, CameraAccess.__table__, AuditLog.__table__,
    ])
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


@pytest.fixture()
def owner(db):
    u = User(id=1, username="owner", email="owner@x.com", role="reseller")
    db.add(u); db.commit()
    return u


@pytest.fixture()
def guest(db):
    u = User(id=2, username="guest", email="guest@x.com", role="end_user")
    db.add(u); db.commit()
    return u


def _create(db, owner, level, is_public=False):
    body = CreateInvitationBody(camera_id=7, camera_name="cam7",
                                permission_level=level, is_public=is_public)
    return create_invitation(body, db=db, current_user=owner)


def test_one_link_per_mode(db, owner):
    """D1：同相機不同模式各自成鏈，同模式重複建立回收舊連結。"""
    a = _create(db, owner, "full")
    b = _create(db, owner, "stream_only")
    assert a["token"] != b["token"]
    again = _create(db, owner, "full")
    assert again["token"] == a["token"] and again.get("reused") is True


def test_public_only_for_stream_only(db, owner):
    with pytest.raises(HTTPException) as e:
        _create(db, owner, "full", is_public=True)
    assert e.value.status_code == 400


def test_unknown_level_rejected(db, owner):
    with pytest.raises(HTTPException) as e:
        _create(db, owner, "root")
    assert e.value.status_code == 400


def test_accept_stamps_invitation_id(db, owner, guest):
    inv = _create(db, owner, "photos_stream")
    accept_invitation(inv["token"], db=db, current_user=guest)
    acc = db.query(CameraAccess).filter_by(user_id=guest.id, camera_id=7).one()
    assert acc.permission_level == "photos_stream"
    assert acc.invitation_id == inv["id"]


def test_accept_latest_wins_up_and_down(db, owner, guest):
    """定案②：重複接受以最新為準（可升可降）。"""
    full_inv = _create(db, owner, "full")
    preview_inv = _create(db, owner, "stream_only")
    accept_invitation(full_inv["token"], db=db, current_user=guest)
    accept_invitation(preview_inv["token"], db=db, current_user=guest)  # 降級
    acc = db.query(CameraAccess).filter_by(user_id=guest.id, camera_id=7).one()
    assert acc.permission_level == "stream_only"
    assert acc.invitation_id == preview_inv["id"]
    accept_invitation(full_inv["token"], db=db, current_user=guest)  # 升回
    acc = db.query(CameraAccess).filter_by(user_id=guest.id, camera_id=7).one()
    assert acc.permission_level == "full"


def test_revoke_only_kills_own_grants(db, owner, guest):
    """D2：撤「僅預覽」連結不影響同相機「全功能」授權。"""
    full_inv = _create(db, owner, "full")
    preview_inv = _create(db, owner, "stream_only")
    other = User(id=3, username="other", email="other@x.com", role="end_user")
    db.add(other); db.commit()
    accept_invitation(full_inv["token"], db=db, current_user=guest)
    accept_invitation(preview_inv["token"], db=db, current_user=other)
    cancel_invitation(preview_inv["id"], db=db, current_user=owner)
    remaining = db.query(CameraAccess).filter_by(camera_id=7).all()
    assert [a.user_id for a in remaining] == [guest.id]


def test_revoke_legacy_rows_by_level(db, owner, guest):
    """舊資料（invitation_id=NULL）以 granted_by＋permission_level fallback。"""
    inv = _create(db, owner, "stream_only")
    db.add(CameraAccess(camera_id=7, user_id=guest.id, granted_by=owner.id,
                        permission_level="stream_only", invitation_id=None))
    db.commit()
    cancel_invitation(inv["id"], db=db, current_user=owner)
    assert db.query(CameraAccess).filter_by(camera_id=7).count() == 0
