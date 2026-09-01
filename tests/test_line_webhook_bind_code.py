"""webhook 綁定碼分支：有效碼綁定＋設 active、過期/無效回覆、重複綁定、多對多。"""
from datetime import datetime, timedelta

import pytest

from models import User, UserLineAccount, LineBindCode


@pytest.fixture(autouse=True)
def _no_line_api(monkeypatch):
    """測試不打 LINE API：profile 一律回固定值。"""
    import routers.line_webhook as wh

    async def fake_profile(line_user_id):
        return {"displayName": "測試小明", "pictureUrl": "https://p.example/x.jpg"}
    monkeypatch.setattr(wh, "_fetch_line_profile", fake_profile)


def _mk_user(db, name):
    u = User(username=name, email=f"{name}@x.com", hashed_password="h", role="end_user")
    db.add(u); db.flush()
    return u


def _mk_code(db, user, code="123456", minutes=10):
    row = LineBindCode(code=code, user_id=user.id,
                       expires_at=datetime.utcnow() + timedelta(minutes=minutes))
    db.add(row); db.commit()
    return row


@pytest.mark.anyio
async def test_not_a_code_returns_none(db):
    from routers.line_webhook import _handle_bind_code
    assert await _handle_bind_code(db, "Ux", "你好") is None
    assert await _handle_bind_code(db, "Ux", "12345") is None      # 5 位
    assert await _handle_bind_code(db, "Ux", "1234567") is None    # 7 位


@pytest.mark.anyio
async def test_valid_code_binds_and_activates(db):
    from routers.line_webhook import _handle_bind_code
    u = _mk_user(db, "bindme")
    _mk_code(db, u, "111222")
    reply = await _handle_bind_code(db, "U-new", "111222")
    assert "綁定成功" in reply and "bindme" in reply
    row = db.query(UserLineAccount).filter_by(user_id=u.id, line_user_id="U-new").one()
    assert row.is_active is True
    assert row.display_name == "測試小明"
    code_row = db.query(LineBindCode).filter_by(code="111222").one()
    assert code_row.used_at is not None


@pytest.mark.anyio
async def test_second_account_same_line_switches_active(db):
    from routers.line_webhook import _handle_bind_code
    a, b = _mk_user(db, "acc-a"), _mk_user(db, "acc-b")
    _mk_code(db, a, "111111")
    await _handle_bind_code(db, "U-shared", "111111")
    _mk_code(db, b, "222222")
    reply = await _handle_bind_code(db, "U-shared", "222222")
    assert "綁定成功" in reply and "acc-b" in reply
    rows = db.query(UserLineAccount).filter_by(line_user_id="U-shared").all()
    assert len(rows) == 2
    active = [r for r in rows if r.is_active]
    assert len(active) == 1 and active[0].user_id == b.id


@pytest.mark.anyio
async def test_expired_or_unknown_code_rejected(db):
    from routers.line_webhook import _handle_bind_code
    u = _mk_user(db, "expired")
    _mk_code(db, u, "333444", minutes=-1)
    assert "無效或已過期" in await _handle_bind_code(db, "Ux", "333444")
    assert "無效或已過期" in await _handle_bind_code(db, "Ux", "999999")
    assert db.query(UserLineAccount).count() == 0


@pytest.mark.anyio
async def test_rebind_same_pair_sets_active_no_duplicate(db):
    from routers.line_webhook import _handle_bind_code
    a, b = _mk_user(db, "dup-a"), _mk_user(db, "dup-b")
    _mk_code(db, a, "111111")
    await _handle_bind_code(db, "U-dup", "111111")
    _mk_code(db, b, "222222")
    await _handle_bind_code(db, "U-dup", "222222")   # active 移到 b
    _mk_code(db, a, "333333")
    reply = await _handle_bind_code(db, "U-dup", "333333")  # a 已綁過 → 切回 active
    assert "已綁定" in reply
    rows = db.query(UserLineAccount).filter_by(line_user_id="U-dup").all()
    assert len(rows) == 2
    active = [r for r in rows if r.is_active]
    assert len(active) == 1 and active[0].user_id == a.id
