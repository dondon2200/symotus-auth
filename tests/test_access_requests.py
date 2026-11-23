"""相機存取權限請求（2026-11-23）：查詢、送出、核准、拒絕與可見範圍。"""
import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from models import (
    AuditLog, Base, CameraAccess, CameraAccessRequest, User, UserLineAccount,
)
from routers import access_requests as ar
from services import access_request_notifier as notifier

CAMERAS = [
    {"id": 7, "name": "北屯工地 A 棟", "ip_address": "192.168.108.1", "port": 8181, "device_serial_id": "2025061200099"},
    {"id": 8, "name": "埔心公宅", "ip_address": "192.168.109.1", "port": 8181, "device_serial_id": None},
]


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[
        User.__table__, CameraAccess.__table__, CameraAccessRequest.__table__,
        AuditLog.__table__, UserLineAccount.__table__,
    ])
    s = sessionmaker(bind=engine)()

    async def _cams():
        return CAMERAS
    monkeypatch.setattr(ar, "_all_cameras", _cams)
    monkeypatch.setattr(ar, "assert_camera_unlocked", lambda *a, **k: None)
    monkeypatch.setattr(ar, "_rate_limit", lambda *a, **k: None)
    yield s
    s.close()


def _user(db, uid, role="end_user"):
    u = User(id=uid, username=f"u{uid}", email=f"u{uid}@x.com", role=role, is_active=True)
    db.add(u); db.commit()
    return u


def _req():
    return Request({"type": "http", "headers": [], "client": ("1.2.3.4", 1)})


def _run(coro):
    return asyncio.run(coro)


def _grant(db, camera_id, user_id, level="full"):
    db.add(CameraAccess(camera_id=camera_id, user_id=user_id, granted_by=user_id,
                        permission_level=level, invitation_id=0))
    db.commit()


def _lookup(db, user, **kw):
    return _run(ar.lookup_camera(ar.LookupBody(**kw), _req(), db=db, current_user=user))


def _create(db, user, camera_id=7, note=None):
    return _run(ar.create_access_request(ar.CreateRequestBody(camera_id=camera_id, note=note),
                                         _req(), db=db, current_user=user))


# ── 查詢 ──────────────────────────────────────────────────────────────────────
def test_lookup_not_exists(db):
    assert _lookup(db, _user(db, 1), ip_address="10.0.0.9", port=8181) == {"exists": False}


def test_lookup_has_access_returns_name(db):
    u = _user(db, 1); _grant(db, 7, 1, "photos_stream")
    r = _lookup(db, u, ip_address="192.168.108.1", port=8181)
    assert r == {"exists": True, "camera_id": 7, "has_access": True, "camera_name": "北屯工地 A 棟"}


def test_lookup_no_access_hides_name(db):
    r = _lookup(db, _user(db, 1), ip_address="192.168.108.1", port=8181)
    assert r == {"exists": True, "camera_id": 7, "has_access": False, "request_pending": False}
    assert "camera_name" not in r


def test_lookup_serial_wins_over_ip(db):
    """序號比得到就用序號，即使 IP 指向另一台。"""
    r = _lookup(db, _user(db, 1, "symotus_admin"), ip_address="192.168.109.1", port=8181, serial="2025061200099")
    assert r["camera_id"] == 7 and r["has_access"] is True


def test_lookup_port_must_match(db):
    assert _lookup(db, _user(db, 1), ip_address="192.168.108.1", port=80) == {"exists": False}


def test_lookup_requires_ip_or_serial(db):
    with pytest.raises(HTTPException) as e:
        _lookup(db, _user(db, 1))
    assert e.value.status_code == 400


# ── 送出 ──────────────────────────────────────────────────────────────────────
def test_create_and_dedupe(db):
    u = _user(db, 1)
    a = _create(db, u, note="  我是現場工程師 ")
    assert a["status"] == "pending"
    row = db.query(CameraAccessRequest).one()
    assert row.camera_name == "北屯工地 A 棟" and row.note == "我是現場工程師"
    again = _create(db, u)
    assert again == {"id": a["id"], "status": "pending", "reused": True}
    assert db.query(CameraAccessRequest).count() == 1
    assert _lookup(db, u, ip_address="192.168.108.1", port=8181)["request_pending"] is True
    assert db.query(AuditLog).filter(AuditLog.action == "camera_access_request").count() == 1


def test_create_rejects_when_already_has_access(db):
    u = _user(db, 1); _grant(db, 7, 1, "stream_only")
    with pytest.raises(HTTPException) as e:
        _create(db, u)
    assert e.value.status_code == 400 and e.value.detail["code"] == "ALREADY_HAS_ACCESS"


def test_create_unknown_camera_404(db):
    with pytest.raises(HTTPException) as e:
        _create(db, _user(db, 1), camera_id=999)
    assert e.value.status_code == 404


# ── 可核准的人與可見範圍 ──────────────────────────────────────────────────────
def test_approvers_are_full_holders_and_admins(db):
    _user(db, 1); _user(db, 2); _user(db, 3); _user(db, 9, "symotus_admin")
    _grant(db, 7, 2, "full"); _grant(db, 7, 3, "photos_stream")
    assert ar.approver_ids(db, 7) == [2, 9]


def test_incoming_scope(db):
    requester = _user(db, 1); owner = _user(db, 2); other = _user(db, 3)
    admin = _user(db, 9, "symotus_admin")
    _grant(db, 7, 2, "full"); _grant(db, 8, 3, "full"); _grant(db, 7, 3, "photos_stream")
    _create(db, requester, 7)

    mine = ar.list_incoming(db=db, current_user=owner)
    assert [r["camera_id"] for r in mine] == [7]
    assert mine[0]["requester_name"] == "u1" and mine[0]["requester_email"] == "u1@x.com"
    assert ar.list_incoming(db=db, current_user=other) == []       # 只有 photos_stream，看不到
    assert ar.list_incoming(db=db, current_user=requester) == []
    assert len(ar.list_incoming(db=db, current_user=admin)) == 1


# ── 核准／拒絕 ────────────────────────────────────────────────────────────────
def test_approve_grants_chosen_level(db):
    requester = _user(db, 1); owner = _user(db, 2, "reseller"); _grant(db, 7, 2, "full")
    rid = _create(db, requester)["id"]
    out = _run(ar.approve_access_request(rid, ar.ApproveBody(permission_level="stream_only"),
                                         db=db, current_user=owner))
    assert out["status"] == "approved"
    acc = db.query(CameraAccess).filter(CameraAccess.user_id == 1, CameraAccess.camera_id == 7).one()
    assert (acc.permission_level, acc.granted_by, acc.invitation_id) == ("stream_only", 2, 0)
    assert db.query(User).get(1).reseller_id == 2          # 從屬樹回填
    assert ar.list_incoming(db=db, current_user=owner) == []
    done = ar.list_incoming(status="all", db=db, current_user=owner)[0]
    assert done["status"] == "approved" and done["decided_by_name"] == "u2"


def test_approve_twice_409_and_bad_level_400(db):
    requester = _user(db, 1); admin = _user(db, 9, "symotus_admin")
    rid = _create(db, requester)["id"]
    with pytest.raises(HTTPException) as e:
        _run(ar.approve_access_request(rid, ar.ApproveBody(permission_level="root"), db=db, current_user=admin))
    assert e.value.status_code == 400
    _run(ar.approve_access_request(rid, ar.ApproveBody(), db=db, current_user=admin))
    with pytest.raises(HTTPException) as e:
        _run(ar.approve_access_request(rid, ar.ApproveBody(), db=db, current_user=admin))
    assert e.value.status_code == 409 and e.value.detail["code"] == "REQUEST_ALREADY_DECIDED"


def test_non_approver_gets_404(db):
    """沒有全功能管理權限的人不該知道請求存在；請求者自己也不能核准自己。"""
    requester = _user(db, 1); viewer = _user(db, 3); _grant(db, 7, 3, "photos_stream")
    rid = _create(db, requester)["id"]
    for who in (viewer, requester):
        with pytest.raises(HTTPException) as e:
            _run(ar.approve_access_request(rid, ar.ApproveBody(), db=db, current_user=who))
        assert e.value.status_code == 404
        with pytest.raises(HTTPException) as e:
            _run(ar.reject_access_request(rid, db=db, current_user=who))
        assert e.value.status_code == 404
    assert db.query(CameraAccess).filter(CameraAccess.user_id == 1).count() == 0


def test_reject_grants_nothing_and_allows_new_request(db):
    requester = _user(db, 1); admin = _user(db, 9, "symotus_admin")
    rid = _create(db, requester)["id"]
    assert _run(ar.reject_access_request(rid, db=db, current_user=admin))["status"] == "rejected"
    assert db.query(CameraAccess).count() == 0
    assert _create(db, requester)["id"] != rid          # 被拒絕後可以再送一次


# ── LINE 通知 ─────────────────────────────────────────────────────────────────
def test_notify_payloads_only_active_line(db):
    _user(db, 1); _user(db, 2)
    db.add_all([
        UserLineAccount(user_id=2, line_user_id="Uactive", is_active=True),
        UserLineAccount(user_id=2, line_user_id="Uold", is_active=False),
    ]); db.commit()
    ps = notifier.new_request_payloads(db, [1, 2], "小明", "北屯工地 A 棟")
    assert [p["to"] for p in ps] == ["Uactive"]
    flex = ps[0]["messages"][0]
    assert "小明" in flex["contents"]["body"]["contents"][1]["text"]
    assert flex["contents"]["footer"]["contents"][0]["action"]["uri"].endswith("/shares?tab=requests")

    ok = notifier.decision_payloads(db, 2, "北屯工地 A 棟", True, 7)
    assert ok[0]["messages"][0]["contents"]["footer"]["contents"][0]["action"]["uri"].endswith("/camera/7")
    assert notifier.decision_payloads(db, 1, "x", False, 7) == []     # 沒綁 LINE 就不推
