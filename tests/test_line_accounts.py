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


# ── DELETE /auth/me/line/{id} 與 /auth/me 的多帳號回報（原在 test_line_bind.py，
#    與已停用的 OAuth 綁定流程無關，搬到此檔保留覆蓋） ──────────────────────────

def test_unlink_specific_line_account_removes_only_that_one(client, make_user, auth_headers, db):
    user = make_user("lineunlink", "lineunlink@example.com", password="oldpassword")
    a = UserLineAccount(user_id=user.id, line_user_id="line-uid-x", display_name="X")
    b = UserLineAccount(user_id=user.id, line_user_id="line-uid-y", display_name="Y")
    db.add_all([a, b])
    db.commit()
    db.refresh(a)
    db.refresh(b)

    r = client.delete(f"/auth/me/line/{a.id}", headers=auth_headers(user))
    assert r.status_code == 200

    rows = db.query(UserLineAccount).filter(UserLineAccount.user_id == user.id).all()
    assert len(rows) == 1
    assert rows[0].id == b.id


def test_unlink_line_account_after_removing_all_leaves_list_empty(client, make_user, auth_headers, db):
    user = make_user("lineunlinkall", "lineunlinkall@example.com", password="oldpassword")
    a = UserLineAccount(user_id=user.id, line_user_id="line-uid-z")
    db.add(a)
    db.commit()
    db.refresh(a)

    r = client.delete(f"/auth/me/line/{a.id}", headers=auth_headers(user))
    assert r.status_code == 200

    body = client.get("/auth/me", headers=auth_headers(user)).json()
    assert body["line_accounts"] == []
    assert body["line_linked"] is False


def test_unlink_line_account_not_owned_by_user_404s(client, make_user, auth_headers, db):
    owner = make_user("lineownerx", "lineownerx@example.com", password="oldpassword")
    other = make_user("lineotherx", "lineotherx@example.com", password="oldpassword")
    a = UserLineAccount(user_id=owner.id, line_user_id="line-uid-w")
    db.add(a)
    db.commit()
    db.refresh(a)

    r = client.delete(f"/auth/me/line/{a.id}", headers=auth_headers(other))
    assert r.status_code == 404
    assert db.query(UserLineAccount).filter(UserLineAccount.id == a.id).first() is not None


def test_unlink_line_account_unknown_id_404s(client, make_user, auth_headers):
    user = make_user("lineunknown", "lineunknown@example.com", password="oldpassword")
    r = client.delete("/auth/me/line/999999", headers=auth_headers(user))
    assert r.status_code == 404


def test_me_reports_multiple_line_accounts(client, make_user, auth_headers, db):
    user = make_user("linemany", "linemany@example.com", password="oldpassword")
    db.add_all([
        UserLineAccount(user_id=user.id, line_user_id="line-uid-1", display_name="One"),
        UserLineAccount(user_id=user.id, line_user_id="line-uid-2", display_name="Two"),
    ])
    db.commit()

    body = client.get("/auth/me", headers=auth_headers(user)).json()
    assert body["line_linked"] is True
    assert len(body["line_accounts"]) == 2
    names = {a["display_name"] for a in body["line_accounts"]}
    assert names == {"One", "Two"}
    for a in body["line_accounts"]:
        assert "id" in a and "created_at" in a
