"""LINE Login 一鍵綁定：綁定 session 與 OAuth callback。"""
import secrets
from datetime import datetime, timedelta

from models import LineBindSession


def test_bind_session_model_roundtrip(db, make_user):
    user = make_user("bs0", "bs0@x.com", password="password123")
    row = LineBindSession(sid="abc123", user_id=user.id,
                          expires_at=datetime.utcnow() + timedelta(minutes=5))
    db.add(row)
    db.commit()
    got = db.query(LineBindSession).filter_by(sid="abc123").one()
    assert got.user_id == user.id and got.used_at is None
    assert got.user.username == "bs0"
