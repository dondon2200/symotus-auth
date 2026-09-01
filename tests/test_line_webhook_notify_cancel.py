"""FIX 3 回歸測試：「取消相機通知」要對這支 LINE 綁定的所有 Symotus 帳號生效。

情境：個人帳號 P、工作帳號 W 都綁到同一支 LINE，兩者都有相機 #7 的存取權。
使用者在推播訊息點「取消相機通知」時，_resolve_user 解出的是目前作用中的
那個帳號（例如 P）；但通知是推給「所有綁定帳號」的聯集，只關掉 P 的
notify_on_online 不夠——W 名下若還開著，下次開機還是會推播，跟「已取消」
的回覆矛盾。
"""
from models import User, UserLineAccount, CameraAccess
from routers.line_webhook import _silence_camera_for_all_bound_accounts


def _bind(db, name, line_uid, active=False):
    u = User(username=name, email=f"{name}@x.com", hashed_password="h", role="end_user")
    db.add(u); db.flush()
    db.add(UserLineAccount(user_id=u.id, line_user_id=line_uid, is_active=active))
    db.commit()
    return u


def test_silences_every_account_bound_to_the_line(db):
    p = _bind(db, "cancel-p", "U-cancel", active=True)
    w = _bind(db, "cancel-w", "U-cancel", active=False)
    for u in (p, w):
        db.add(CameraAccess(camera_id=7, user_id=u.id, granted_by=u.id,
                            permission_level="full", notify_on_online=True, invitation_id=0))
    db.commit()

    _silence_camera_for_all_bound_accounts(db, "U-cancel", 7)
    db.commit()

    rows = db.query(CameraAccess).filter(CameraAccess.camera_id == 7).all()
    assert {r.user_id for r in rows} == {p.id, w.id}
    assert all(r.notify_on_online is False for r in rows)


def test_creates_sentinel_row_for_account_with_no_existing_access(db):
    """其中一個綁定帳號名下完全沒有這台相機的 camera_access 列（例如 admin 全域收通知）
    ——要補一列退訂標記，跟原本單帳號版本的 0-d 行為一致。"""
    a = _bind(db, "sentinel-a", "U-sentinel", active=True)
    b = _bind(db, "sentinel-b", "U-sentinel", active=False)
    db.add(CameraAccess(camera_id=9, user_id=a.id, granted_by=a.id,
                        permission_level="full", notify_on_online=True, invitation_id=0))
    db.commit()

    _silence_camera_for_all_bound_accounts(db, "U-sentinel", 9)
    db.commit()

    rows = db.query(CameraAccess).filter(CameraAccess.camera_id == 9).all()
    assert {r.user_id for r in rows} == {a.id, b.id}
    assert all(r.notify_on_online is False for r in rows)
    b_row = next(r for r in rows if r.user_id == b.id)
    assert b_row.invitation_id == 0


def test_only_touches_accounts_bound_to_this_line(db):
    """不相干的第三個帳號（綁在別支 LINE）不該被動到。"""
    a = _bind(db, "iso-a", "U-iso-1", active=True)
    c = _bind(db, "iso-c", "U-iso-2", active=True)
    for u in (a, c):
        db.add(CameraAccess(camera_id=11, user_id=u.id, granted_by=u.id,
                            permission_level="full", notify_on_online=True, invitation_id=0))
    db.commit()

    _silence_camera_for_all_bound_accounts(db, "U-iso-1", 11)
    db.commit()

    a_row = db.query(CameraAccess).filter_by(camera_id=11, user_id=a.id).one()
    c_row = db.query(CameraAccess).filter_by(camera_id=11, user_id=c.id).one()
    assert a_row.notify_on_online is False
    assert c_row.notify_on_online is True
