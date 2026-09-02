"""相機分享自助建帳（spec 2026-09-02 S1-S5）。sqlite in-memory 只建需要的表。"""
import asyncio
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
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
    """403 擋下後不該留副作用：不建立任何新 User，signup_count 也不遞增。"""
    out = _create(db, reseller, "full", invitee_email="bob@example.com")
    user_count_before = db.query(User).count()
    with pytest.raises(HTTPException) as e:
        _signup(db, out["token"], email="eve@example.com", username="eve")
    assert e.value.status_code == 403
    assert "限定特定 Email" in e.value.detail
    assert db.query(User).count() == user_count_before
    inv = db.query(CameraInvitation).filter_by(token=out["token"]).first()
    assert (inv.signup_count or 0) == 0


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
        _signup(db, out["token"], email=f"u{i}@x.com", username=f"usr{i}", ip=f"198.51.100.{i}")
    with pytest.raises(HTTPException) as e:
        _signup(db, out["token"], email="u10@x.com", username="usr10", ip="198.51.100.99")
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
        _signup(db, out["token"], email="x@x.com", username="usrx")
    assert e.value.status_code == 400
    assert "不需要帳號" in e.value.detail


def test_signup_rejects_revoked_link(db, reseller):
    out = _create(db, reseller, "photos_stream")
    inv = db.query(CameraInvitation).filter_by(token=out["token"]).first()
    inv.status = "revoked"; db.commit()
    with pytest.raises(HTTPException) as e:
        _signup(db, inv.token, email="x@x.com", username="usrx")
    assert e.value.status_code == 400
    assert "撤銷" in e.value.detail


def test_signup_rejects_short_password():
    """密碼過短由 pydantic 在建構 InviteSignupBody 時就擋下，根本不會呼叫到
    signup_via_invitation。直接鎖住 8 這個下限邊界（7 碼被擋、8 碼通過），
    而非僅斷言與 Field(min_length=8) 同義的字串——這樣往後若有人把 min_length
    改小，這條測試會確實變紅。"""
    with pytest.raises(ValidationError):
        InviteSignupBody(username="usrx", email="x@x.com", password="1234567")
    InviteSignupBody(username="usrx", email="x@x.com", password="12345678")


def test_signup_rate_limited_per_ip(db, reseller):
    """同 IP 每分鐘 5 次；第 6 次回 429（每次都用新 email 避免撞到別的錯誤）。"""
    out = _create(db, reseller, "photos_stream")
    for i in range(5):
        _signup(db, out["token"], email=f"r{i}@x.com", username=f"rate{i}", ip="192.0.2.7")
    with pytest.raises(HTTPException) as e:
        _signup(db, out["token"], email="r5@x.com", username="rate5", ip="192.0.2.7")
    assert e.value.status_code == 429


def test_inherit_reseller_chain(db):
    admin = User(id=90, username="ad", email="ad@x.com", role="symotus_admin")
    rs = User(id=91, username="r2", email="r2@x.com", role="reseller")
    eu = User(id=92, username="e2", email="e2@x.com", role="end_user", reseller_id=91)
    db.add_all([admin, rs, eu]); db.commit()
    assert _inherit_reseller_id(admin) is None
    assert _inherit_reseller_id(rs) == 91
    assert _inherit_reseller_id(eu) == 91        # end_user 再分享 → 沿用其上層


# ── 審查修補：保留身分 / 輸入驗證 / 交易一致性（Task 5 review）──────────────────


def test_signup_rejects_reserved_admin_email(db, reseller):
    """免登入建帳絕不能冒用 admin@timelapse.com——那是 Camera Backend 用來辨識
    symotus_admin 的保留 email，用它建帳可能換到看得見全部相機的 token。"""
    out = _create(db, reseller, "photos_stream")
    with pytest.raises(HTTPException) as e:
        _signup(db, out["token"], email="admin@timelapse.com", username="hijack1")
    assert e.value.status_code == 400
    assert "不可用於註冊" in e.value.detail


def test_signup_rejects_reserved_line_alias_email(db, reseller):
    """line_*@symotus.com 是 LINE 帳號的內部別名，同樣不可被自助建帳冒用。"""
    out = _create(db, reseller, "photos_stream")
    with pytest.raises(HTTPException) as e:
        _signup(db, out["token"], email="line_U123@symotus.com", username="hijack2")
    assert e.value.status_code == 400
    assert "不可用於註冊" in e.value.detail


def test_signup_body_rejects_invalid_email_format():
    """email 格式錯誤要在 pydantic 建構 body 時就被擋下（EmailStr），
    根本進不了 signup_via_invitation。"""
    with pytest.raises(ValidationError):
        InviteSignupBody(username="usr1", email="not-an-email", password="pw12345678")


def test_signup_body_rejects_role_field_injection():
    """extra='forbid'：body 夾帶 role 這種伺服器才該決定的欄位要直接被拒絕，
    而不是被 pydantic 默默丟棄後續才被忽略——建構當下就要炸。"""
    with pytest.raises(ValidationError):
        InviteSignupBody(
            username="usr1", email="usr1@x.com", password="pw12345678",
            role="symotus_admin",
        )


def test_signup_rejects_expired_link(db, reseller):
    """過期連結目前完全沒有測試覆蓋；signup 端點也要擋過期，不只 accept/preview。"""
    from datetime import datetime, timedelta
    out = _create(db, reseller, "photos_stream")
    inv = db.query(CameraInvitation).filter_by(token=out["token"]).first()
    inv.expires_at = datetime.utcnow() - timedelta(hours=1)
    db.commit()
    with pytest.raises(HTTPException) as e:
        _signup(db, inv.token, email="x@x.com", username="usrx")
    assert e.value.status_code == 400
    assert "過期" in e.value.detail


def test_signup_stores_email_as_lowercase(db, reseller):
    """修正 1：email 一律存正規化後的小寫。登入已改為大小寫不敏感比對
    （routers/auth.py：func.lower(User.email) == ...），所以這裡不必再遷就
    「存原樣才能用原樣大小寫登入」的舊限制；統一存小寫可避免同一 email
    因大小寫不同被誤判成兩個不同帳號。"""
    out = _create(db, reseller, "photos_stream")
    _signup(db, out["token"], email="Bob2@Example.com", username="bob2user")
    user = db.query(User).filter_by(username="bob2user").first()
    assert user is not None
    assert user.email == "bob2@example.com"
    assert user.email == user.email.casefold()


# ── 修正 1 回歸測試：登入大小寫不敏感（routers/auth.py）─────────────────────


import routers.auth as auth_mod
from auth import hash_password as _hash_password
from schemas import LoginRequest


@pytest.fixture(autouse=True)
def stub_auth_camera_token(monkeypatch):
    """login() 也會呼叫 get_camera_token 換 Camera Backend token；
    測試不需要真的打外部網路，直接短路回空字典。"""
    async def _fake(user_id, email, role, camera_email=None):
        return {}
    monkeypatch.setattr(auth_mod, "get_camera_token", _fake)


def test_login_allows_case_insensitive_email(db, reseller):
    """修正 1 核心保障：email 大小寫不同也要能登入。

    建帳時存的是正規化小寫 email（見 test_signup_stores_email_as_lowercase），
    但使用者未必記得自己當初輸入的大小寫，甚至輸入法/瀏覽器自動大寫首字母都
    可能讓登入輸入與儲存值不同。這條測試確保「輸入大小寫不同於儲存值」時仍
    能登入成功——這正是本次修法要解決的問題（過去只有唯一一種、使用者無從
    得知的大小寫形式能登入）。
    """
    user = User(username="caseuser", email="caseuser@example.com",
                hashed_password=_hash_password("pw12345678"),
                role="end_user", is_active=True)
    db.add(user); db.commit()

    body = LoginRequest(username="CaseUser@Example.com", password="pw12345678")
    result = asyncio.run(auth_mod.login(body, FakeRequest(ip="203.0.113.77"), db=db))
    assert result.access_token


# ── Task 5：username 撞號自動改名，email 撞號才擋下 ──────────────────────────


@pytest.fixture()
def alice(db):
    """username 為 alice 的既有帳號（模擬 A 先用 alice@gmail.com 建帳）。"""
    u = User(id=3, username="alice", email="alice@gmail.com", role="end_user")
    db.add(u); db.commit()
    return u


def test_signup_username_collision_auto_renames(db, reseller, alice):
    """B 拿到另一條邀請、用 alice@company.com 建帳：email 從沒出現過，
    但推導出的 username "alice" 已被 A 佔用。修法前這裡會 400 並叫他登入，
    但他根本沒有帳號——永久卡死。修法後應自動改名、成功建帳。"""
    out = _create(db, reseller, "photos_stream")
    res = _signup(db, out["token"], email="alice@company.com", username="alice")
    assert res["access_token"]
    user = db.query(User).filter_by(email="alice@company.com").first()
    assert user is not None
    assert user.username != "alice"
    assert user.username.startswith("alice")


def test_signup_username_collision_chain_picks_next_available(db, reseller, alice):
    """alice、alice2 都已存在時，第三人建帳仍要成功，且拿到再下一個可用名字。"""
    taken2 = User(id=4, username="alice2", email="alice2@somewhere.com", role="end_user")
    db.add(taken2); db.commit()

    out = _create(db, reseller, "photos_stream")
    res = _signup(db, out["token"], email="alice@thirdcompany.com", username="alice")
    assert res["access_token"]
    user = db.query(User).filter_by(email="alice@thirdcompany.com").first()
    assert user is not None
    assert user.username not in ("alice", "alice2")
    assert user.username.startswith("alice")


def test_signup_email_collision_still_blocked_with_email_specific_message(db, reseller, alice):
    """email 本身撞號（含大小寫不同）仍然是真的該叫他登入的情況，維持擋下。"""
    out = _create(db, reseller, "photos_stream")
    with pytest.raises(HTTPException) as e:
        _signup(db, out["token"], email="Alice@Gmail.com", username="whatever")
    assert e.value.status_code == 400
    assert "Email" in e.value.detail
    assert "登入" in e.value.detail


def test_signup_renamed_username_respects_max_length(db, reseller):
    """改名後的 username 仍須符合 InviteSignupBody 的 max_length=64 上限。"""
    base = "a" * 60  # 接近 64 字元的 local part
    taken = User(id=5, username=base, email=f"{base}@existing.com", role="end_user")
    db.add(taken); db.commit()

    out = _create(db, reseller, "photos_stream")
    res = _signup(db, out["token"], email=f"{base}@newcompany.com", username=base)
    assert res["access_token"]
    user = db.query(User).filter_by(email=f"{base}@newcompany.com").first()
    assert user is not None
    assert user.username != base
    assert len(user.username) <= 64


def test_login_username_side_stays_case_sensitive(db):
    """修正 1 的邊界保障：只有 email 側改成大小寫不敏感，username 側維持精確比對。

    DB 裡存的 username 是小寫 "caseuser"；用大小寫不同的 "CaseUser" 當帳號輸入
    登入，不該命中（不然就是 username 側也被誤放寬成不敏感了）。這是與
    test_login_allows_case_insensitive_email 對照的反例：email 側放寬、
    username 側不放寬，兩者都要鎖住。
    """
    user = User(username="caseuser", email="caseuser2@example.com",
                hashed_password=_hash_password("pw12345678"),
                role="end_user", is_active=True)
    db.add(user); db.commit()

    body = LoginRequest(username="CaseUser", password="pw12345678")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(auth_mod.login(body, FakeRequest(ip="203.0.113.77"), db=db))
    assert exc_info.value.status_code == 401
