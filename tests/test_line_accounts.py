from models import User, UserLineAccount


def test_user_can_have_multiple_line_accounts(db):
    u = User(username="multi", email="multi@x.com", hashed_password="h", role="end_user")
    db.add(u); db.flush()
    db.add_all([
        UserLineAccount(user_id=u.id, line_user_id="U111"),
        UserLineAccount(user_id=u.id, line_user_id="U222"),
    ])
    db.commit()
    assert {a.line_user_id for a in u.line_accounts} == {"U111", "U222"}


def test_same_line_can_bind_multiple_users(db):
    """多對多：同一個 LINE 可綁到多個 Symotus 帳號。"""
    a = User(username="a1", email="a1@x.com", hashed_password="h", role="end_user")
    b = User(username="b1", email="b1@x.com", hashed_password="h", role="end_user")
    db.add_all([a, b]); db.flush()
    db.add(UserLineAccount(user_id=a.id, line_user_id="U333")); db.commit()
    db.add(UserLineAccount(user_id=b.id, line_user_id="U333")); db.commit()
    assert {r.user_id for r in db.query(UserLineAccount)
            .filter(UserLineAccount.line_user_id == "U333")} == {a.id, b.id}


def test_same_user_same_line_pair_unique(db):
    """同一 (帳號, LINE) 組合不可重複。"""
    import pytest
    from sqlalchemy.exc import IntegrityError
    a = User(username="a2", email="a2@x.com", hashed_password="h", role="end_user")
    db.add(a); db.flush()
    db.add(UserLineAccount(user_id=a.id, line_user_id="U444")); db.commit()
    db.add(UserLineAccount(user_id=a.id, line_user_id="U444"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_line_bind_code_model_roundtrip(db):
    from datetime import datetime, timedelta
    from models import LineBindCode
    u = User(username="c1", email="c1@x.com", hashed_password="h", role="end_user")
    db.add(u); db.flush()
    db.add(LineBindCode(code="123456", user_id=u.id,
                        expires_at=datetime.utcnow() + timedelta(minutes=10)))
    db.commit()
    row = db.query(LineBindCode).filter(LineBindCode.code == "123456").first()
    assert row.user_id == u.id and row.used_at is None
