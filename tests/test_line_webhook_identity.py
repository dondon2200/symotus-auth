"""Task 4：LINE 助手身分解析與通知推播改查 user_line_accounts，支援多 LINE。"""
import pytest

from models import User, UserLineAccount
from routers.line_webhook import _resolve_user
import services.camera_notifier as camera_notifier


def test_resolve_user_two_line_accounts_same_user(db):
    u = User(username="multi", email="multi@x.com", hashed_password="h", role="end_user")
    db.add(u); db.flush()
    db.add_all([
        UserLineAccount(user_id=u.id, line_user_id="U111"),
        UserLineAccount(user_id=u.id, line_user_id="U222"),
    ])
    db.commit()

    assert _resolve_user(db, "U111").id == u.id
    assert _resolve_user(db, "U222").id == u.id


def test_resolve_user_unbound_returns_none(db):
    assert _resolve_user(db, "U-does-not-exist") is None


@pytest.mark.anyio
async def test_notify_pushes_to_all_bound_line_accounts(db, monkeypatch):
    u = User(username="notifyme", email="notifyme@x.com", hashed_password="h", role="end_user")
    db.add(u); db.flush()
    db.add_all([
        UserLineAccount(user_id=u.id, line_user_id="U111"),
        UserLineAccount(user_id=u.id, line_user_id="U222"),
    ])
    db.commit()

    from models import CameraAccess
    db.add(CameraAccess(
        camera_id=1, user_id=u.id, granted_by=u.id, permission_level="full",
        notify_on_online=True, invitation_id=0,
    ))
    db.commit()

    pushed = []

    async def fake_send_line_push(line_user_id, camera_id, camera_name):
        pushed.append((line_user_id, camera_id, camera_name))

    monkeypatch.setattr(camera_notifier, "send_line_push", fake_send_line_push)

    line_ids = camera_notifier.get_notify_line_ids(1, db)
    assert set(line_ids) == {"U111", "U222"}

    for lid in line_ids:
        await camera_notifier.send_line_push(lid, 1, "測試相機")

    assert {p[0] for p in pushed} == {"U111", "U222"}
    assert len(pushed) == 2
