"""盤點/停用「OAuth-only 舊帳號」。

判定條件（符合任一即算）：
- hashed_password IS NULL（從未設過密碼，純 OAuth 帳號）
- email LIKE '%@oauth.local'（舊版合成 email）
- camera_email LIKE 'line_%@symotus.com'（LINE 合成 Camera Backend 帳號）

用法：
    python scripts/deactivate_oauth_accounts.py --dry-run   # 只列出，不改資料
    python scripts/deactivate_oauth_accounts.py             # 實際將符合者 is_active=False
"""
import argparse
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import or_
from sqlalchemy.orm import Session

from database import SessionLocal
from models import User, CameraAccess


def find_oauth_only_accounts(db: Session):
    """回傳符合「OAuth-only 舊帳號」條件的 User 清單。"""
    return db.query(User).filter(
        or_(
            User.hashed_password.is_(None),
            User.email.like("%@oauth.local"),
            User.camera_email.like("line_%@symotus.com"),
        )
    ).all()


def describe_account(db: Session, user: User) -> dict:
    """組出盤點用的顯示資訊（含名下 camera_access）。"""
    accesses = db.query(CameraAccess).filter(CameraAccess.user_id == user.id).all()
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "camera_email": user.camera_email,
        "camera_access": [a.camera_id for a in accesses],
    }


def deactivate_oauth_only_accounts(db: Session, dry_run: bool = False):
    """核心函式：找出符合條件的帳號；dry_run=False 時一併停用（is_active=False）並 commit。

    回傳盤點清單（list[dict]），供 --dry-run 列印或測試斷言。
    """
    users = find_oauth_only_accounts(db)
    report = [describe_account(db, u) for u in users]
    if not dry_run:
        for u in users:
            u.is_active = False
        db.commit()
    return report


def _print_report(report: list[dict]) -> None:
    if not report:
        print("沒有符合條件的 OAuth-only 舊帳號。")
        return
    for row in report:
        print(
            f"id={row['id']} username={row['username']} email={row['email']} "
            f"camera_email={row['camera_email']} camera_access={row['camera_access']}"
        )
    print(f"共 {len(report)} 筆")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只列出，不修改資料")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        report = deactivate_oauth_only_accounts(db, dry_run=args.dry_run)
        _print_report(report)
        if not args.dry_run:
            print("已將以上帳號設為 is_active=False。")
    finally:
        db.close()


if __name__ == "__main__":
    main()
