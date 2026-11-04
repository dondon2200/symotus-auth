"""角色政策接線（invites／invitations／users／jobs／support）：
預設行為與改版前一致，翻轉列後改變。Depends(require_feature) 的路由用「取出依賴直接呼叫」驗證。"""
import asyncio
import inspect
import pytest
from fastapi import HTTPException

from models import RolePolicy, CameraAccess
from role_policies import invalidate_role_cache
import routers.invites as invites_mod
import routers.invitations as invitations_mod
import routers.users as users_mod
import routers.jobs as jobs_mod
import routers.support as support_mod
from schemas import InviteCreate


class U:
    def __init__(self, role, id=1, jobs_view_all=False):
        self.role, self.id, self.jobs_view_all, self.username = role, id, jobs_view_all, f"u{id}"


class Row:
    def __init__(self, key, role, allowed):
        self.feature_key, self.role, self.allowed = key, role, allowed


class Q:
    def __init__(self, rows):
        self._rows, self.filters = rows, 0
    def filter(self, *a, **k):
        self.filters += 1
        return self
    def first(self): return self._rows[0] if self._rows else None
    def all(self): return self._rows
    def update(self, *a, **k): return 0


class Db:
    def __init__(self, role_rows=(), access=None, invitation=None):
        self.role_rows, self.access, self.invitation = list(role_rows), access, invitation
        self.last_q = None
    def query(self, model):
        if model is RolePolicy: return Q(self.role_rows)
        if model is CameraAccess: return Q([self.access] if self.access else [])
        self.last_q = Q([self.invitation] if self.invitation else [])
        return self.last_q
    def add(self, *a): pass
    def commit(self): pass
    def refresh(self, *a): pass
    @property
    def session(self): return self


@pytest.fixture(autouse=True)
def _cache():
    invalidate_role_cache()
    yield
    invalidate_role_cache()


def _dep(fn, name="current_user"):
    return inspect.signature(fn).parameters[name].default.dependency


def _expect_403(dep, user, db=None):
    with pytest.raises(HTTPException) as e:
        dep(current_user=user, db=db or Db())
    assert e.value.status_code == 403


# ── require_feature 掛在路由上 ─────────────────────────────────
@pytest.mark.parametrize("fn,key", [
    (invites_mod.create_invite, "account.invite"),
    (invites_mod.list_invites, "account.invite"),
    (invites_mod.revoke_invite, "account.invite"),
    (users_mod.reseller_create_user, "account.create"),
    (support_mod.create_grant, "support.grant"),
    (support_mod.revoke_grant, "support.grant"),
])
def test_routes_guarded_by_feature(fn, key):
    dep = _dep(fn)
    _expect_403(dep, U("end_user"))
    assert dep(current_user=U("reseller"), db=Db()).role == "reseller"
    invalidate_role_cache()
    _expect_403(dep, U("reseller"), Db([Row(key, "reseller", False)]))
    invalidate_role_cache()
    assert dep(current_user=U("end_user"), db=Db([Row(key, "end_user", True)])).role == "end_user"



# ── admin 在 reseller 專屬端點維持 403（改版前即如此）──────────────
@pytest.mark.parametrize("fn", [
    users_mod.reseller_create_user,
    support_mod.create_grant,
    support_mod.revoke_grant,
])
def test_reseller_only_routes_block_admin(fn):
    dep = _dep(fn)
    assert dep.admin_allowed is False
    _expect_403(dep, U("symotus_admin"))


@pytest.mark.parametrize("fn", [
    invites_mod.create_invite,
    invites_mod.list_invites,
    invites_mod.revoke_invite,
])
def test_invite_routes_still_allow_admin(fn):
    dep = _dep(fn)
    assert dep.admin_allowed is True
    assert dep(current_user=U("symotus_admin"), db=Db()).role == "symotus_admin"

# ── account.invite_reseller ────────────────────────────────────
def test_reseller_invite_requires_flag():
    body = InviteCreate(email="x@y.com", intended_role="reseller", expires_hours=24)
    with pytest.raises(HTTPException) as e:
        invites_mod.create_invite(body, db=Db(), current_user=U("reseller"))
    assert e.value.status_code == 403
    invalidate_role_cache()
    out = invites_mod.create_invite(body, db=Db([Row("account.invite_reseller", "reseller", True)]), current_user=U("reseller"))
    assert out["intended_role"] == "reseller"
    assert invites_mod.create_invite(body, db=Db(), current_user=U("symotus_admin"))["intended_role"] == "reseller"


# ── invitation.revoke_any ──────────────────────────────────────
class _Inv:
    def __init__(self): self.id, self.status, self.email, self.intended_role = 1, "pending", "", "end_user"


def test_revoke_invite_scopes_to_own_unless_flag(monkeypatch):
    monkeypatch.setattr(invites_mod, "log_action", lambda *a, **k: None)
    db = Db(invitation=_Inv())
    invites_mod.revoke_invite("tok", db=db, current_user=U("reseller"))
    assert db.last_q.filters == 2   # token/status 一次 ＋ reseller_id 一次
    invalidate_role_cache()
    db = Db([Row("invitation.revoke_any", "reseller", True)], invitation=_Inv())
    invites_mod.revoke_invite("tok", db=db, current_user=U("reseller"))
    assert db.last_q.filters == 1


def test_cancel_invitation_scope_helper():
    q = Q([])
    assert invitations_mod._scope_cancel(q, Db(), U("end_user")) is q and q.filters == 1
    q2 = Q([])
    invitations_mod._scope_cancel(q2, Db(), U("symotus_admin"))
    assert q2.filters == 0
    invalidate_role_cache()
    q3 = Q([])
    invitations_mod._scope_cancel(q3, Db([Row("invitation.revoke_any", "reseller", True)]), U("reseller"))
    assert q3.filters == 0


# ── camera.share.own ───────────────────────────────────────────
class _Access:
    def __init__(self, granted_by, level): self.granted_by, self.permission_level = granted_by, level


def test_assert_can_share():
    invitations_mod._assert_can_share(Db(), U("reseller"), 7)                                  # 預設放行
    with pytest.raises(HTTPException):
        invitations_mod._assert_can_share(Db(), U("end_user"), 7)                               # 無授權
    invitations_mod._assert_can_share(Db(access=_Access(99, "full")), U("end_user"), 7)        # 被分享 full 照舊可再分享
    invalidate_role_cache()
    with pytest.raises(HTTPException):
        invitations_mod._assert_can_share(Db([Row("camera.share.own", "reseller", False)]), U("reseller"), 7)
    invalidate_role_cache()
    invitations_mod._assert_can_share(Db([Row("camera.share.own", "end_user", True)]), U("end_user"), 7)


# ── account.grant_access ───────────────────────────────────────
def test_grant_access_gate():
    with pytest.raises(HTTPException) as e:
        asyncio.run(users_mod.grant_access_to_managed_user({}, db=Db(), current_user=U("end_user")))
    assert e.value.status_code == 403
    # reseller 預設過角色閘，接著撞到 400（camera_id/user_id 必填）＝閘門已放行
    with pytest.raises(HTTPException) as e:
        asyncio.run(users_mod.grant_access_to_managed_user({}, db=Db(), current_user=U("reseller")))
    assert e.value.status_code == 400
    invalidate_role_cache()
    # 兩鍵任一即可：只開 camera.share.own 也過閘
    with pytest.raises(HTTPException) as e:
        asyncio.run(users_mod.grant_access_to_managed_user(
            {}, db=Db([Row("camera.share.own", "end_user", True)]), current_user=U("end_user")))
    assert e.value.status_code == 400


# ── jobs.view_all ──────────────────────────────────────────────
def test_can_view_all_includes_role_policy():
    assert jobs_mod._can_view_all(U("symotus_admin"), Db())
    assert jobs_mod._can_view_all(U("end_user", jobs_view_all=True), Db())
    assert not jobs_mod._can_view_all(U("reseller"), Db())
    invalidate_role_cache()
    assert jobs_mod._can_view_all(U("reseller"), Db([Row("jobs.view_all", "reseller", True)]))
