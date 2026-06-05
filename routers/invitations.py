"""
相機邀請系統（連結式）
Admin/Reseller 產生邀請連結 → 分享給任何人 → 點連結接受
"""
import secrets
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from pydantic import BaseModel
from typing import Optional

from database import get_db
from models import User, CameraInvitation, CameraAccess
from auth import get_current_user, require_role
from config import settings

router = APIRouter(prefix="/invitations", tags=["invitations"])

FRONTEND_URL = getattr(settings, "FRONTEND_URL", "https://admin.symotus.com")


PERMISSION_LABELS = {
    "full": "完整存取（設定＋照片＋串流）",
    "photos_stream": "照片＋串流（不可改設定）",
    "stream_only": "只看串流",
}

class CreateInvitationBody(BaseModel):
    camera_id: int
    camera_name: Optional[str] = None
    note: Optional[str] = None
    permission_level: str = "photos_stream"  # full / photos_stream / stream_only
    expires_hours: Optional[int] = None  # None = 不過期


# ── 建立邀請（產生連結）────────────────────────────────────────────────────────
@router.post("")
def create_invitation(
    body: CreateInvitationBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("reseller", "symotus_admin")),
):
    token = secrets.token_urlsafe(24)
    expires_at = None
    if body.expires_hours:
        expires_at = datetime.utcnow() + timedelta(hours=body.expires_hours)

    inv = CameraInvitation(
        token=token,
        inviter_id=current_user.id,
        camera_id=body.camera_id,
        camera_name=body.camera_name or f"相機 #{body.camera_id}",
        note=body.note,
        permission_level=body.permission_level if body.permission_level in ("full","photos_stream","stream_only") else "photos_stream",
        expires_at=expires_at,
    )
    db.add(inv); db.commit(); db.refresh(inv)

    invite_url = f"{FRONTEND_URL}/camera-invite/{token}"
    return {
        "id": inv.id,
        "token": token,
        "invite_url": invite_url,
        "camera_name": inv.camera_name,
        "expires_at": expires_at.isoformat() if expires_at else None,
    }


# ── 查看邀請資訊（公開，不需登入）──────────────────────────────────────────────
@router.get("")
def list_my_invitations(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """列出我收到的待處理邀請（作為被邀請者）"""
    invs = db.query(CameraInvitation).filter(
        CameraInvitation.invitee_id == current_user.id,
        CameraInvitation.status == "pending",
    ).order_by(CameraInvitation.created_at.desc()).all()
    result = []
    for inv in invs:
        inviter = db.query(User).filter(User.id == inv.inviter_id).first()
        result.append({
            "id": inv.id,
            "camera_id": inv.camera_id,
            "camera_name": inv.camera_name or f"相機 #{inv.camera_id}",
            "inviter_name": (inviter.full_name or inviter.username or inviter.email) if inviter else "未知",
            "permission_level": inv.permission_level,
            "note": inv.note,
            "created_at": inv.created_at.isoformat() if inv.created_at else None,
        })
    return result


@router.get("/preview/{token}")
def preview_invitation(token: str, db: Session = Depends(get_db)):
    inv = db.query(CameraInvitation).filter(CameraInvitation.token == token).first()
    if not inv:
        return {"valid": False, "reason": "not_found"}
    if inv.status != "pending":
        return {"valid": False, "reason": inv.status}
    if inv.expires_at and inv.expires_at < datetime.utcnow():
        return {"valid": False, "reason": "expired"}

    inviter = db.query(User).filter(User.id == inv.inviter_id).first()
    return {
        "valid": True,
        "camera_id": inv.camera_id,
        "camera_name": inv.camera_name,
        "inviter_name": inviter.full_name or inviter.username or inviter.email if inviter else "管理員",
        "note": inv.note,
        "permission_level": inv.permission_level,
        "permission_label": PERMISSION_LABELS.get(inv.permission_level, ""),
        "expires_at": inv.expires_at.isoformat() if inv.expires_at else None,
    }


# ── 接受邀請（需登入）──────────────────────────────────────────────────────────
@router.post("/accept/{token}")
def accept_invitation(
    token: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    inv = db.query(CameraInvitation).filter(CameraInvitation.token == token).first()
    if not inv:
        raise HTTPException(404, "邀請連結不存在")
    if inv.status != "pending":
        raise HTTPException(400, f"此邀請已{inv.status}")
    if inv.expires_at and inv.expires_at < datetime.utcnow():
        raise HTTPException(400, "邀請連結已過期")

    # 確認未重複授權
    existing = db.query(CameraAccess).filter(
        CameraAccess.user_id == current_user.id,
        CameraAccess.camera_id == inv.camera_id,
    ).first()
    if not existing:
        db.add(CameraAccess(camera_id=inv.camera_id, user_id=current_user.id, granted_by=inv.inviter_id, permission_level=inv.permission_level))

    inv.status = "accepted"
    inv.invitee_id = current_user.id
    inv.responded_at = datetime.utcnow()
    db.commit()

    return {"message": f"已接受！相機「{inv.camera_name}」已加入您的儀表板", "camera_id": inv.camera_id}


# ── 拒絕邀請（需登入）──────────────────────────────────────────────────────────
@router.post("/decline/{token}")
def decline_invitation(
    token: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    inv = db.query(CameraInvitation).filter(CameraInvitation.token == token).first()
    if not inv:
        raise HTTPException(404, "邀請連結不存在")
    if inv.status != "pending":
        raise HTTPException(400, "此邀請已處理")

    inv.status = "declined"
    inv.invitee_id = current_user.id
    inv.responded_at = datetime.utcnow()
    db.commit()
    return {"message": "已拒絕邀請"}


# ── 查看已送出的邀請──────────────────────────────────────────────────────────
@router.get("/sent")
def list_sent_invitations(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("reseller", "symotus_admin")),
):
    invs = db.query(CameraInvitation).filter(
        CameraInvitation.inviter_id == current_user.id,
    ).order_by(CameraInvitation.created_at.desc()).limit(50).all()

    result = []
    for inv in invs:
        invitee = db.query(User).filter(User.id == inv.invitee_id).first() if inv.invitee_id else None
        result.append({
            "id": inv.id, "token": inv.token, "camera_id": inv.camera_id,
            "camera_name": inv.camera_name, "status": inv.status,
            "invite_url": f"{FRONTEND_URL}/camera-invite/{inv.token}",
            "created_at": inv.created_at.isoformat() if inv.created_at else None,
            "permission_level": inv.permission_level,
            "permission_label": PERMISSION_LABELS.get(inv.permission_level, ""),
            "invitee_name": (invitee.full_name or invitee.username or invitee.email) if invitee else None,
            "expires_at": inv.expires_at.isoformat() if inv.expires_at else None,
        })
    return result


# ── 取消邀請──────────────────────────────────────────────────────────────────
@router.delete("/{inv_id}")
def cancel_invitation(
    inv_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("reseller", "symotus_admin")),
):
    inv = db.query(CameraInvitation).filter(
        CameraInvitation.id == inv_id,
        CameraInvitation.inviter_id == current_user.id,
        CameraInvitation.status.in_(["pending", "accepted"]),
    ).first()
    if not inv:
        raise HTTPException(404, "邀請不存在或無法撤銷")
    if inv.status == "accepted" and inv.invitee_id:
        db.query(CameraAccess).filter(
            CameraAccess.camera_id == inv.camera_id,
            CameraAccess.user_id == inv.invitee_id,
            CameraAccess.granted_by == current_user.id,
        ).delete()
    inv.status = "revoked"; db.commit()
    return {"message": "已停止分享"}
