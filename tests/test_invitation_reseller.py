"""分享邀請時 reseller 從屬回填的回歸測試。

背景（routers/invitations.py:194）：
- end_user 若無 reseller_id，接受 reseller/admin 的邀請後自動掛到該邀請者旗下
- 若已有 reseller_id，則不覆寫（保留現有從屬）
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, User, CameraInvitation, CameraAccess, AuditLog, FeaturePolicy
from policies import invalidate_cache
from routers.invitations import CreateInvitationBody, create_invitation, accept_invitation


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[
        User.__table__, CameraInvitation.__table__, CameraAccess.__table__,
        AuditLog.__table__, FeaturePolicy.__table__,
    ])
    invalidate_cache()
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    invalidate_cache()


@pytest.fixture()
def reseller_r1(db):
    """第一個 reseller，ID=1"""
    u = User(id=1, username="reseller_r1", email="r1@x.com", role="reseller")
    db.add(u)
    db.commit()
    return u


@pytest.fixture()
def reseller_r2(db):
    """第二個 reseller，ID=2"""
    u = User(id=2, username="reseller_r2", email="r2@x.com", role="reseller")
    db.add(u)
    db.commit()
    return u


@pytest.fixture()
def end_user_with_reseller(db, reseller_r1):
    """end_user 已有 reseller_id，指向 R1"""
    u = User(id=10, username="user_with_r1", email="user_with_r1@x.com",
             role="end_user", reseller_id=reseller_r1.id)
    db.add(u)
    db.commit()
    return u


@pytest.fixture()
def end_user_without_reseller(db):
    """end_user 沒有 reseller_id（為 None）"""
    u = User(id=20, username="user_no_reseller", email="user_no_reseller@x.com",
             role="end_user", reseller_id=None)
    db.add(u)
    db.commit()
    return u


def _create_invitation(db, inviter, camera_id=7, level="photos_stream"):
    """輔助函式：建立邀請"""
    body = CreateInvitationBody(
        camera_id=camera_id, camera_name=f"cam{camera_id}",
        permission_level=level
    )
    return create_invitation(body, db=db, current_user=inviter)


def test_accept_invitation_does_not_overwrite_existing_reseller_id(
    db, reseller_r1, reseller_r2, end_user_with_reseller
):
    """
    已有 reseller_id=R1 的 end_user，接受 R2 發出的邀請後，
    reseller_id 應保持為 R1，不被 R2 覆寫。
    """
    # 驗證初始狀態
    db.refresh(end_user_with_reseller)
    assert end_user_with_reseller.reseller_id == reseller_r1.id

    # R2 建立邀請
    inv = _create_invitation(db, reseller_r2, camera_id=7, level="photos_stream")

    # end_user_with_reseller 接受 R2 的邀請
    accept_invitation(inv["token"], db=db, current_user=end_user_with_reseller)

    # 驗證：reseller_id 應保持為 R1，而非被改成 R2
    db.refresh(end_user_with_reseller)
    assert end_user_with_reseller.reseller_id == reseller_r1.id, \
        f"Expected reseller_id to remain {reseller_r1.id}, but got {end_user_with_reseller.reseller_id}"

    # 驗證：camera_access 正確建立
    acc = db.query(CameraAccess).filter_by(
        user_id=end_user_with_reseller.id, camera_id=7
    ).one()
    assert acc.permission_level == "photos_stream"
    assert acc.granted_by == reseller_r2.id  # 分享者正確


def test_accept_invitation_fills_empty_reseller_id(
    db, reseller_r1, end_user_without_reseller
):
    """
    reseller_id=None 的 end_user，接受 reseller 的邀請後，
    reseller_id 應回填為邀請者（inviter）的 ID。
    """
    # 驗證初始狀態
    db.refresh(end_user_without_reseller)
    assert end_user_without_reseller.reseller_id is None

    # R1 建立邀請
    inv = _create_invitation(db, reseller_r1, camera_id=8, level="photos_stream")

    # end_user_without_reseller 接受邀請
    accept_invitation(inv["token"], db=db, current_user=end_user_without_reseller)

    # 驗證：reseller_id 應回填為 R1（邀請者）
    db.refresh(end_user_without_reseller)
    assert end_user_without_reseller.reseller_id == reseller_r1.id, \
        f"Expected reseller_id to be filled with {reseller_r1.id}, but got {end_user_without_reseller.reseller_id}"

    # 驗證：camera_access 正確建立
    acc = db.query(CameraAccess).filter_by(
        user_id=end_user_without_reseller.id, camera_id=8
    ).one()
    assert acc.permission_level == "photos_stream"
    assert acc.granted_by == reseller_r1.id


def test_accept_invitation_respects_admin_inviter_for_reseller_fill(
    db, end_user_without_reseller
):
    """
    reseller_id=None 的 end_user 接受 symotus_admin 發出的邀請，
    reseller_id 也應回填為該 admin 的 ID（admin 也滿足 inviter.role 條件）。
    """
    admin = User(id=30, username="admin", email="admin@x.com", role="symotus_admin")
    db.add(admin)
    db.commit()

    # 驗證初始狀態
    db.refresh(end_user_without_reseller)
    assert end_user_without_reseller.reseller_id is None

    # admin 建立邀請
    inv = _create_invitation(db, admin, camera_id=9, level="stream_only")

    # end_user_without_reseller 接受邀請
    accept_invitation(inv["token"], db=db, current_user=end_user_without_reseller)

    # 驗證：reseller_id 應回填為 admin ID
    db.refresh(end_user_without_reseller)
    assert end_user_without_reseller.reseller_id == admin.id, \
        f"Expected reseller_id to be filled with {admin.id}, but got {end_user_without_reseller.reseller_id}"


def test_accept_invitation_no_fill_if_inviter_is_end_user(db):
    """
    end_user A（無 reseller_id）接受 end_user B 的邀請，
    reseller_id 不應被回填（邀請者不是 reseller/admin）。

    在此測試中，reseller_r 先給 user_b 分享（full），user_b 再轉分享給 user_a。
    user_a 接受後，reseller_id 不應回填（因為邀請者 user_b 不是 reseller/admin）。
    """
    reseller_r = User(id=31, username="reseller_r", email="r@x.com", role="reseller")
    user_a = User(id=40, username="user_a", email="user_a@x.com",
                  role="end_user", reseller_id=None)
    user_b = User(id=41, username="user_b", email="user_b@x.com",
                  role="end_user", reseller_id=None)

    db.add(reseller_r)
    db.add(user_a)
    db.add(user_b)
    db.commit()

    # Step 1: reseller_r 給 user_b 分享（full access）
    inv_for_b = _create_invitation(db, reseller_r, camera_id=10, level="full")
    accept_invitation(inv_for_b["token"], db=db, current_user=user_b)

    # Step 2: user_b 基於上述 full grant 再分享給 user_a
    inv = _create_invitation(db, user_b, camera_id=10, level="stream_only")

    # Step 3: user_a 接受 user_b 的邀請
    accept_invitation(inv["token"], db=db, current_user=user_a)

    # 驗證：user_a 的 reseller_id 仍為 None（未被回填，因為邀請者 user_b 是 end_user）
    db.refresh(user_a)
    assert user_a.reseller_id is None, \
        f"Expected reseller_id to remain None (inviter is end_user), but got {user_a.reseller_id}"
