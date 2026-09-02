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


from routers.invitations import accept_invitation


@pytest.fixture()
def guest(db):
    u = User(id=2, username="guest", email="Bob@Example.com", role="end_user")
    db.add(u); db.commit()
    return u


def test_accept_rejects_wrong_email(db, reseller, guest):
    out = _create(db, reseller, "full", invitee_email="someone-else@x.com")
    with pytest.raises(HTTPException) as e:
        accept_invitation(out["token"], db=db, current_user=guest)
    assert e.value.status_code == 403


def test_accept_allows_matching_email_case_insensitive(db, reseller, guest):
    out = _create(db, reseller, "full", invitee_email="bob@example.com")
    accept_invitation(out["token"], db=db, current_user=guest)
    acc = db.query(CameraAccess).filter_by(user_id=guest.id, camera_id=7).first()
    assert acc is not None and acc.permission_level == "full"


def test_accept_unrestricted_link_unaffected(db, reseller, guest):
    out = _create(db, reseller, "photos_stream")
    accept_invitation(out["token"], db=db, current_user=guest)
    assert db.query(CameraAccess).filter_by(user_id=guest.id).count() == 1


from routers.invitations import preview_invitation, _mask_email


def test_mask_email():
    assert _mask_email("bob@example.com") == "b***@example.com"


def test_preview_reports_signup_state(db, reseller):
    out = _create(db, reseller, "full", invitee_email="bob@example.com")
    p = preview_invitation(out["token"], db=db)
    assert p["signup_allowed"] is True
    assert p["signup_exhausted"] is False
    assert p["invitee_email_masked"] == "b***@example.com"
    assert "bob@example.com" not in str(p)      # 原始 email 不得外流


def test_preview_marks_exhausted(db, reseller):
    out = _create(db, reseller, "full", invitee_email="bob@example.com")
    inv = db.query(CameraInvitation).filter_by(token=out["token"]).first()
    inv.signup_count = 1        # limit 為 1，已用完
    db.commit()
    p = preview_invitation(inv.token, db=db)
    assert p["signup_allowed"] is False
    assert p["signup_exhausted"] is True


def test_preview_public_link_has_no_signup(db, reseller):
    out = _create(db, reseller, "stream_only", is_public=True)
    p = preview_invitation(out["token"], db=db)
    assert p["signup_allowed"] is False
    assert p["invitee_email_masked"] is None


import routers.invitations as inv_mod
from routers.invitations import signup_via_invitation, InviteSignupBody, _inherit_reseller_id


class FakeRequest:
    """_rate_limit 只用到 headers 與 client。"""
    def __init__(self, ip="203.0.113.9"):
        self.headers = {"x-forwarded-for": ip}
        self.client = None


@pytest.fixture(autouse=True)
def stub_camera_token(monkeypatch):
    async def _fake(user_id, email, role, camera_email=None):
        return {"access_token": "cam-a", "refresh_token": "cam-r"}
    monkeypatch.setattr(inv_mod, "get_camera_token", _fake)


@pytest.fixture(autouse=True)
def clear_rate_limit():
    """_rl_buckets 是 routers/auth.py 的模組級全域，會跨測試累積；
    不清會讓同 IP 的第 6 次呼叫吃到 429，測試莫名其妙變紅。"""
    from routers.auth import _rl_buckets
    _rl_buckets.clear()
    yield
    _rl_buckets.clear()


def _signup(db, token, email="bob@example.com", username="bob",
            password="pw12345678", ip="203.0.113.9"):
    body = InviteSignupBody(username=username, email=email, password=password, full_name="Bob")
    return asyncio.run(signup_via_invitation(token, body, FakeRequest(ip), db=db))


def test_signup_creates_user_and_grant(db, reseller):
    out = _create(db, reseller, "full", invitee_email="bob@example.com")
    res = _signup(db, out["token"])
    user = db.query(User).filter_by(username="bob").first()
    assert user.role == "end_user"
    assert user.is_active is True
    assert user.camera_email is None
    assert user.created_by == reseller.id
    assert user.reseller_id == reseller.id          # 分享者是 reseller → 掛其名下
    acc = db.query(CameraAccess).filter_by(user_id=user.id, camera_id=7).first()
    assert acc.permission_level == "full"
    assert acc.granted_by == reseller.id
    inv = db.query(CameraInvitation).filter_by(token=out["token"]).first()
    assert acc.invitation_id == inv.id
    assert inv.signup_count == 1 and inv.invitee_id == user.id and inv.status == "accepted"
    assert res["access_token"] and res["camera_access_token"] == "cam-a"
    assert res["camera_id"] == 7


def test_signup_rejects_wrong_email(db, reseller):
    out = _create(db, reseller, "full", invitee_email="bob@example.com")
    with pytest.raises(HTTPException) as e:
        _signup(db, out["token"], email="eve@example.com", username="eve")
    assert e.value.status_code == 403


def test_signup_respects_quota(db, reseller):
    out = _create(db, reseller, "full", invitee_email="bob@example.com")
    _signup(db, out["token"])
    with pytest.raises(HTTPException) as e:
        _signup(db, out["token"], username="bob2", ip="203.0.113.10")
    assert e.value.status_code == 400
    assert "名額" in e.value.detail


def test_signup_open_link_default_limit_ten(db, reseller):
    out = _create(db, reseller, "photos_stream")
    for i in range(10):
        _signup(db, out["token"], email=f"u{i}@x.com", username=f"u{i}", ip=f"198.51.100.{i}")
    with pytest.raises(HTTPException) as e:
        _signup(db, out["token"], email="u10@x.com", username="u10", ip="198.51.100.99")
    assert e.value.status_code == 400


def test_signup_existing_email_tells_user_to_login(db, reseller, guest):
    out = _create(db, reseller, "photos_stream")
    with pytest.raises(HTTPException) as e:
        _signup(db, out["token"], email="bob@example.com", username="other")  # guest 已用此 email
    assert e.value.status_code == 400
    assert "登入" in e.value.detail


def test_signup_blocked_on_public_link(db, reseller):
    out = _create(db, reseller, "stream_only", is_public=True)
    with pytest.raises(HTTPException) as e:
        _signup(db, out["token"], email="x@x.com", username="x")
    assert e.value.status_code == 400


def test_signup_rejects_revoked_link(db, reseller):
    out = _create(db, reseller, "photos_stream")
    inv = db.query(CameraInvitation).filter_by(token=out["token"]).first()
    inv.status = "revoked"; db.commit()
    with pytest.raises(HTTPException) as e:
        _signup(db, inv.token, email="x@x.com", username="x")
    assert e.value.status_code == 400


def test_signup_rejects_short_password(db, reseller):
    out = _create(db, reseller, "photos_stream")
    with pytest.raises(HTTPException) as e:
        _signup(db, out["token"], email="x@x.com", username="x", password="short")
    assert e.value.status_code == 400


def test_signup_rate_limited_per_ip(db, reseller):
    """同 IP 每分鐘 5 次；第 6 次回 429（每次都用新 email 避免撞到別的錯誤）。"""
    out = _create(db, reseller, "photos_stream")
    for i in range(5):
        _signup(db, out["token"], email=f"r{i}@x.com", username=f"r{i}", ip="192.0.2.7")
    with pytest.raises(HTTPException) as e:
        _signup(db, out["token"], email="r5@x.com", username="r5", ip="192.0.2.7")
    assert e.value.status_code == 429


def test_inherit_reseller_chain(db):
    admin = User(id=90, username="ad", email="ad@x.com", role="symotus_admin")
    rs = User(id=91, username="r2", email="r2@x.com", role="reseller")
    eu = User(id=92, username="e2", email="e2@x.com", role="end_user", reseller_id=91)
    db.add_all([admin, rs, eu]); db.commit()
    assert _inherit_reseller_id(admin) is None
    assert _inherit_reseller_id(rs) == 91
    assert _inherit_reseller_id(eu) == 91        # end_user 再分享 → 沿用其上層
