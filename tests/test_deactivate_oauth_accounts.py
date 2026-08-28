"""Task 6：盤點/停用 OAuth-only 舊帳號腳本。"""
from scripts.deactivate_oauth_accounts import deactivate_oauth_only_accounts


def test_dry_run_lists_but_does_not_modify(db, make_user):
    oauth_user = make_user("oauth1", "oauth1@example.com")  # 無密碼 = OAuth-only
    normal_user = make_user("normal1", "normal1@example.com", password="somepassword")

    report = deactivate_oauth_only_accounts(db, dry_run=True)

    ids = {row["id"] for row in report}
    assert oauth_user.id in ids
    assert normal_user.id not in ids

    db.refresh(oauth_user)
    db.refresh(normal_user)
    assert oauth_user.is_active is True
    assert normal_user.is_active is True


def test_real_run_deactivates_only_oauth_only_accounts(db, make_user):
    oauth_user = make_user("oauth2", "oauth2@example.com")  # 無密碼 = OAuth-only
    normal_user = make_user("normal2", "normal2@example.com", password="somepassword")

    report = deactivate_oauth_only_accounts(db, dry_run=False)

    ids = {row["id"] for row in report}
    assert oauth_user.id in ids
    assert normal_user.id not in ids

    db.refresh(oauth_user)
    db.refresh(normal_user)
    assert oauth_user.is_active is False
    assert normal_user.is_active is True


def test_matches_oauth_local_email_and_line_camera_email(db, make_user):
    oauth_local = make_user("oauth3", "oauth3@oauth.local", password="somepassword")
    line_user = make_user("lineuser1", "lineuser1@example.com", password="somepassword")
    line_user.camera_email = "line_abc123@symotus.com"
    db.commit()

    report = deactivate_oauth_only_accounts(db, dry_run=True)

    ids = {row["id"] for row in report}
    assert oauth_local.id in ids
    assert line_user.id in ids
