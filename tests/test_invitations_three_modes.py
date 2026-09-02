"""三模式邀請邏輯（spec D1/D2＋定案②）。sqlite in-memory 只建需要的表。"""
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, User, CameraInvitation, CameraAccess, AuditLog, FeaturePolicy
from policies import invalidate_cache
from routers.invitations import (
    CreateInvitationBody, create_invitation, accept_invitation, cancel_invitation,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[
        User.__table__, CameraInvitation.__table__, CameraAccess.__table__,
        AuditLog.__table__, FeaturePolicy.__table__,  # D4 再分享會查 camera.share 政策
    ])
    invalidate_cache()  # 政策表為空 → level_allows 走 FEATURE_DEFAULTS fallback
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    invalidate_cache()


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


def _create(db, owner, level, is_public=False, **kw):
    if level == "full" and "invitee_email" not in kw:
        kw["invitee_email"] = "invitee@x.com"   # spec 2026-09-02 S2：full 必須指定對象
    body = CreateInvitationBody(camera_id=7, camera_name="cam7",
                                permission_level=level, is_public=is_public, **kw)
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
    full_inv = _create(db, owner, "full", invitee_email=guest.email)
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
    full_inv = _create(db, owner, "full", invitee_email=guest.email)
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


def test_revoke_spares_self_paired_rows(db, owner, guest):
    """I4：自我配對列（user_id==granted_by，訂閱通知自動建立、invitation_id=NULL）
    即使相機與等級都與被撤銷的連結相同，也不得被 NULL fallback 誤刪。"""
    inv = _create(db, owner, "stream_only")
    accept_invitation(inv["token"], db=db, current_user=guest)
    db.add(CameraAccess(camera_id=7, user_id=owner.id, granted_by=owner.id,
                        permission_level="stream_only", invitation_id=None))
    db.commit()
    cancel_invitation(inv["id"], db=db, current_user=owner)
    remaining = db.query(CameraAccess).filter_by(camera_id=7).all()
    assert [a.user_id for a in remaining] == [owner.id]  # 被分享者刪除、自我配對列保留


# ── D4：full 被分享者可再分享（I7）─────────────────────────────────
def test_full_sharee_can_reshare(db, owner, guest):
    db.add(CameraAccess(camera_id=7, user_id=guest.id, granted_by=owner.id,
                        permission_level="full", invitation_id=None))
    db.commit()
    out = _create(db, guest, "stream_only")
    assert out["token"]


def test_photos_stream_sharee_cannot_reshare(db, owner, guest):
    db.add(CameraAccess(camera_id=7, user_id=guest.id, granted_by=owner.id,
                        permission_level="photos_stream", invitation_id=None))
    db.commit()
    with pytest.raises(HTTPException) as e:
        _create(db, guest, "stream_only")
    assert e.value.status_code == 403


def test_no_grant_cannot_reshare(db, owner, guest):
    with pytest.raises(HTTPException) as e:
        _create(db, guest, "stream_only")
    assert e.value.status_code == 403


def test_end_user_can_cancel_own_link_but_not_others(db, owner, guest):
    """D4 補遺：end_user 再分享者可撤自己發的連結（連帶移除其授權）；
    其他 end_user 撤不到（404，受 inviter_id 過濾）。"""
    db.add(CameraAccess(camera_id=7, user_id=guest.id, granted_by=owner.id,
                        permission_level="full", invitation_id=None))
    db.commit()
    inv = _create(db, guest, "stream_only")  # guest 憑 full grant 再分享
    other = User(id=3, username="other", email="other@x.com", role="end_user")
    db.add(other); db.commit()
    accept_invitation(inv["token"], db=db, current_user=other)
    # 別的 end_user 不能撤 guest 發的連結
    stranger = User(id=4, username="stranger", email="stranger@x.com", role="end_user")
    db.add(stranger); db.commit()
    with pytest.raises(HTTPException) as e:
        cancel_invitation(inv["id"], db=db, current_user=stranger)
    assert e.value.status_code == 404
    # 發連結的 end_user 本人可撤，且由此連結建立的授權被移除
    cancel_invitation(inv["id"], db=db, current_user=guest)
    remaining = db.query(CameraAccess).filter_by(camera_id=7).all()
    assert [a.user_id for a in remaining] == [guest.id]  # 只剩 guest 自己的 full grant
