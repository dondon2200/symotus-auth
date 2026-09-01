"""Task 4：LINE 助手身分解析與通知推播改查 user_line_accounts，支援多 LINE。"""
import pytest

from models import User, UserLineAccount
from routers.line_webhook import _resolve_user, _not_bound_reply_text
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


def test_not_bound_reply_text_guides_to_bind_code(monkeypatch):
    """未綁定引導：帳密登入 + 個人設定產生「綁定碼」並在聊天輸入，不再提綁定連結/LINE 登入。"""
    monkeypatch.setenv("FRONTEND_URL", "https://user.symotus.com")
    text = _not_bound_reply_text()
    assert "用 LINE 登入" not in text
    assert "https://user.symotus.com" in text
    assert "個人設定" in text
    assert "綁定碼" in text


def test_not_bound_reply_text_uses_frontend_url_env(monkeypatch):
    monkeypatch.setenv("FRONTEND_URL", "https://custom.example.com")
    text = _not_bound_reply_text()
    assert "https://custom.example.com" in text


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


def test_notify_dedups_shared_line_across_users(db):
    """同一 LINE 綁兩個帳號、兩帳號都對同相機有通知權：只推一次（set 去重）。"""
    from models import CameraAccess
    a = User(username="dd-a", email="dd-a@x.com", hashed_password="h", role="end_user")
    b = User(username="dd-b", email="dd-b@x.com", hashed_password="h", role="end_user")
    db.add_all([a, b]); db.flush()
    db.add_all([
        UserLineAccount(user_id=a.id, line_user_id="U-dup-notify", is_active=True),
        UserLineAccount(user_id=b.id, line_user_id="U-dup-notify", is_active=False),
    ])
    for u in (a, b):
        db.add(CameraAccess(camera_id=7, user_id=u.id, granted_by=u.id,
                            permission_level="full", notify_on_online=True, invitation_id=0))
    db.commit()

    line_ids = camera_notifier.get_notify_line_ids(7, db)
    assert line_ids == ["U-dup-notify"]
