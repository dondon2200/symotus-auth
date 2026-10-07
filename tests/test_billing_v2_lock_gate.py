"""鎖定閘門（spec §8）：非管理員碰鎖定相機一律 403 CAMERA_LOCKED，且在打 Camera Backend 之前就擋下。

注意：tester 帳號實際是 symotus_admin，不能拿來測 deny path（memory: tester-account-is-admin）；
這裡直接用 reseller／end_user 物件呼叫端點函式。"""
import asyncio
import inspect
import os
from datetime import date

import pytest
from fastapi import HTTPException

import routers.cameras as cameras_mod
from models import (
    BillingPlanV2, BillingSubscriptionV2, CameraAccess, CameraInvitation, User, UserLineAccount,
)
from services.billing_v2 import invalidate_lock_cache

CAM = 44


@pytest.fixture()
def world(db, make_user):
    admin = make_user("adm", "adm@test.com", role="symotus_admin")
    reseller = make_user("res", "res@test.com", role="reseller")
    end_user = make_user("eu", "eu@test.com", role="end_user")
    db.add(CameraAccess(camera_id=CAM, user_id=reseller.id, granted_by=reseller.id,
                        permission_level="full", invitation_id=0))
    db.add(CameraAccess(camera_id=CAM, user_id=end_user.id, granted_by=reseller.id,
                        permission_level="full", invitation_id=0))
    plan = BillingPlanV2(name="月", term="monthly", cycle="monthly", price=1)
    db.add(plan)
    db.commit()
    sub = BillingSubscriptionV2(
        camera_id=CAM, camera_name="cam", camera_serial="SER44", customer_id=reseller.id, plan_id=plan.id,
        plan_name="月", term="monthly", cycle="monthly", price=1, start_date=date(2026, 8, 1),
        anchor_day=1, term_start=date(2026, 8, 1), status="suspended")
    db.add(sub)
    db.commit()
    invalidate_lock_cache()
    return {"admin": admin, "reseller": reseller, "end_user": end_user, "sub": sub}


def _run(coro):
    return asyncio.run(coro)


def _locked(exc_info):
    assert exc_info.value.status_code == 403
    assert isinstance(exc_info.value.detail, dict) and exc_info.value.detail["code"] == "CAMERA_LOCKED"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """任何漏網的 Camera Backend 呼叫都讓測試失敗，而不是真的打出去。"""
    async def boom(*a, **k):
        raise AssertionError("不應該打到 Camera Backend")
    monkeypatch.setattr(cameras_mod, "_get_admin_camera_token", boom)
    monkeypatch.setattr(cameras_mod, "get_camera_backend_token", boom)


class FakeRequest:
    def __init__(self, method="GET", params=None):
        self.method = method
        self.query_params = params or {}

    async def body(self):
        return b"{}"


@pytest.mark.parametrize("role", ["reseller", "end_user"])
def test_相機詳情與代理擋下(db, world, role):
    u = world[role]
    with pytest.raises(HTTPException) as ei:
        _run(cameras_mod.get_camera(CAM, current_user=u, db=db))
    _locked(ei)
    with pytest.raises(HTTPException) as ei:
        _run(cameras_mod.proxy_camera_api(CAM, FakeRequest("PUT"), "timesnap", current_user=u, db=db))
    _locked(ei)
    with pytest.raises(HTTPException) as ei:
        _run(cameras_mod.get_live_frame_url(CAM, current_user=u, db=db))
    _locked(ei)
    with pytest.raises(HTTPException) as ei:
        _run(cameras_mod.subscribe_online_notification(CAM, current_user=u, db=db))
    _locked(ei)
    with pytest.raises(HTTPException) as ei:
        _run(cameras_mod.unbind_camera(CAM, current_user=u, db=db))
    _locked(ei)
    with pytest.raises(HTTPException) as ei:
        _run(cameras_mod.nas_images(FakeRequest(params={"camera_id": str(CAM)}), current_user=u, db=db))
    _locked(ei)
    with pytest.raises(HTTPException) as ei:
        _run(cameras_mod.nas_image(FakeRequest(params={"path": "/homes/firmness/SER44/2026-09-01/1.jpg"}),
                                   current_user=u, db=db))
    _locked(ei)


def test_刪除相機擋下reseller(db, world):
    with pytest.raises(HTTPException) as ei:
        _run(cameras_mod.delete_camera(CAM, confirm=True, current_user=world["reseller"], db=db))
    _locked(ei)


def test_縮圖略過鎖定相機(db, world):
    assert _run(cameras_mod.get_thumbnails(str(CAM), current_user=world["end_user"], db=db)) == {}


def test_解鎖後不擋(db, world):
    from datetime import datetime
    world["sub"].status = "ended"
    world["sub"].lock_released_at = datetime.utcnow()
    db.commit()
    invalidate_lock_cache()
    # 不再丟 CAMERA_LOCKED；接著會去拿 token → 被 _no_network 攔下
    with pytest.raises(AssertionError):
        _run(cameras_mod.get_camera(CAM, current_user=world["reseller"], db=db))


def test_相機清單標記並清掉連線資訊(db, world, monkeypatch):
    async def no_token(u):
        return ""

    async def detail(cid, owner, holder):
        return {"id": cid, "name": "cam", "ip_address": "10.0.0.1", "port": 80, "device_serial_id": "SER44",
                "spark_nas_path": "/x", "online_status": "online"}

    monkeypatch.setattr(cameras_mod, "get_camera_backend_token", no_token)
    monkeypatch.setattr(cameras_mod, "fetch_camera_detail", detail)
    out = _run(cameras_mod.list_cameras(current_user=world["end_user"], db=db))
    cam = out["cameras"][0]
    assert cam["billing"]["locked"] == "suspended"
    assert cam["billing"]["overdue"] is False          # end_user 看不到欠款
    for k in ("ip_address", "port", "device_serial_id", "spark_nas_path"):
        assert k not in cam
    assert cam["name"] == "cam" and cam["online_status"] == "online"


def test_邀請_鎖定相機不可建立分享(db, world):
    from routers.invitations import CreateInvitationBody, create_invitation
    CameraInvitation.__table__.create(bind=db.get_bind(), checkfirst=True)
    body = CreateInvitationBody(camera_id=CAM, permission_level="stream_only")
    with pytest.raises(HTTPException) as ei:
        create_invitation(body, db=db, current_user=world["reseller"])
    _locked(ei)


def test_開機通知只推admin(db, world):
    from services.camera_notifier import get_notify_line_ids
    db.add(UserLineAccount(user_id=world["admin"].id, line_user_id="L-admin"))
    db.add(UserLineAccount(user_id=world["end_user"].id, line_user_id="L-eu"))
    db.commit()
    assert get_notify_line_ids(CAM, db) == ["L-admin"]


# ── 路由盤點：新端點沒登記就失敗 ──

# 路徑含 {camera_id} 的端點中，刻意不加閘門的（附理由）
EXEMPT = {
    ("/cameras/{camera_id}/notify-unsubscribe", "POST"): "退訂只會減少推播，鎖定時也應允許",
    ("/cameras/{camera_id}/notify-status", "GET"): "只回訂閱狀態，不含相機資料",
    ("/cameras/{camera_id}/prepare-timelapse", "POST"): "被 /{camera_id}/{path} catch-all 先攔，該處已有閘門",
    ("/reseller/cameras/{camera_id}/access", "GET"): "只列出授權名單，不含相機資料",
    ("/reseller/cameras/{camera_id}/access/{user_id}", "DELETE"): "撤銷授權只會減少存取",
    ("/admin/camera-access/{camera_id}", "GET"): "symotus_admin 專用，本來就不受鎖定限制",
}
GATE_TOKENS = ("assert_camera_unlocked", "locked_camera_ids", "_get_public_cam")


def test_所有相機路由都有閘門或已登記豁免():
    os.environ.setdefault("CAMERA_SERVICE_KEY", "x")
    from main import app
    missing = []
    for r in app.routes:
        path = getattr(r, "path", "")
        if "{camera_id" not in path and "/cameras/public/{token}" not in path:
            continue
        for m in sorted(getattr(r, "methods", []) or []):
            if (path, m) in EXEMPT:
                continue
            src = inspect.getsource(r.endpoint)
            if not any(t in src for t in GATE_TOKENS):
                missing.append(f"{m} {path}")
    assert not missing, f"這些相機路由沒有計費鎖定閘門，請補上或登記到 EXEMPT：{missing}"
