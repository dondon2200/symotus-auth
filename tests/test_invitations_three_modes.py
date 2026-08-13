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
