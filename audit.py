"""稽核 log 共用寫入。與主操作同一個 db session/commit，不額外 commit。"""
from typing import Optional
from sqlalchemy.orm import Session
from models import AuditLog, User


def log_action(
    db: Session,
    actor: Optional[User],
    action: str,
    target_type: Optional[str] = None,
    target_id: Optional[int] = None,
    detail: Optional[str] = None,
):
    db.add(AuditLog(
        actor_id=actor.id if actor else None,
        actor_username=actor.username if actor else "service-key",
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail=(detail or "")[:500] or None,
    ))
