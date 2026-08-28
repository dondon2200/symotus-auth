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


def test_line_user_id_unique_across_users(db):
    import pytest
    from sqlalchemy.exc import IntegrityError
    a = User(username="a1", email="a1@x.com", hashed_password="h", role="end_user")
    b = User(username="b1", email="b1@x.com", hashed_password="h", role="end_user")
    db.add_all([a, b]); db.flush()
    db.add(UserLineAccount(user_id=a.id, line_user_id="U333")); db.commit()
    db.add(UserLineAccount(user_id=b.id, line_user_id="U333"))
    with pytest.raises(IntegrityError):
        db.commit()
