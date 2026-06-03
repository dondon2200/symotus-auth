"""
相機邀請系統：Admin/Reseller 邀請 Reseller/EndUser 存取相機
需要接受才會生效
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime
from pydantic import BaseModel
from typing import Optional

from database import get_db
from models import User, CameraInvitation, CameraAccess
from auth import get_current_user, require_role

router = APIRouter(prefix="/invitations", tags=["invitations"])

CAMERA_BACKEND_URL = "https://user.symotus.com"
CAMERA_SERVICE_KEY = "9ad3343a32508c209152a450f601b990176fa4d41c94c27330e448b1a86826c2"


class CreateInvitationBody(BaseModel):
    invitee_id: int           # 被邀請用戶的 user_id
    camera_id: int
    camera_name: Optional[str] = None
    note: Optional[str] = None


@router.post("")
def create_invitation(
    body: CreateInvitationBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("reseller", "symotus_admin")),
):
    """Admin 或 Reseller 邀請用戶存取相機"""
    # 確認被邀請者存在
    invitee = db.query(User).filter(User.id == body.invitee_id).first()
    if not invitee:
        raise HTTPException(404, "用戶不存在")

    # 不能邀請自己
    if invitee.id == current_user.id:
        raise HTTPException(400, "不能邀請自己")

    # 確認沒有重複的待處理邀請
    existing = db.query(CameraInvitation).filter(
        CameraInvitation.invitee_id == body.invitee_id,
        CameraInvitation.camera_id == body.camera_id,
        CameraInvitation.status == "pending",
    ).first()
    if existing:
        raise HTTPException(400, "已有待處理的邀請")

    # 確認沒有已存在的 camera_access
    existing_access = db.query(CameraAccess).filter(
        CameraAccess.user_id == body.invitee_id,
        CameraAccess.camera_id == body.camera_id,
    ).first()
    if existing_access:
        raise HTTPException(400, "該用戶已有此相機的存取權限")

    inv = CameraInvitation(
        inviter_id=current_user.id,
        invitee_id=body.invitee_id,
        camera_id=body.camera_id,
        camera_name=body.camera_name,
        note=body.note,
    )
    db.add(inv); db.commit(); db.refresh(inv)
    return {"id": inv.id, "status": "pending", "message": "邀請已送出"}


@router.get("")
def list_my_invitations(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """列出我收到的待處理邀請"""
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
            "inviter_name": inviter.full_name or inviter.username or inviter.email if inviter else "未知",
            "note": inv.note,
            "created_at": inv.created_at.isoformat() if inv.created_at else None,
        })
    return result


@router.put("/{inv_id}/accept")
def accept_invitation(
    inv_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """接受相機存取邀請"""
    inv = db.query(CameraInvitation).filter(
        CameraInvitation.id == inv_id,
        CameraInvitation.invitee_id == current_user.id,
        CameraInvitation.status == "pending",
    ).first()
    if not inv:
        raise HTTPException(404, "邀請不存在或已處理")

    # 建立 camera_access
    access = CameraAccess(
        camera_id=inv.camera_id,
        user_id=current_user.id,
        granted_by=inv.inviter_id,
    )
    db.add(access)

    # 更新邀請狀態
    inv.status = "accepted"
    inv.responded_at = datetime.utcnow()
    db.commit()

    return {"message": f"已接受邀請，相機「{inv.camera_name or inv.camera_id}」已加入您的儀表板"}


@router.put("/{inv_id}/decline")
def decline_invitation(
    inv_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """拒絕相機存取邀請"""
    inv = db.query(CameraInvitation).filter(
        CameraInvitation.id == inv_id,
        CameraInvitation.invitee_id == current_user.id,
        CameraInvitation.status == "pending",
    ).first()
    if not inv:
        raise HTTPException(404, "邀請不存在或已處理")

    inv.status = "declined"
    inv.responded_at = datetime.utcnow()
    db.commit()

    return {"message": "已拒絕邀請"}


@router.get("/sent")
def list_sent_invitations(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("reseller", "symotus_admin")),
):
    """列出我送出的邀請"""
    invs = db.query(CameraInvitation).filter(
        CameraInvitation.inviter_id == current_user.id,
    ).order_by(CameraInvitation.created_at.desc()).all()

    result = []
    for inv in invs:
        invitee = db.query(User).filter(User.id == inv.invitee_id).first()
        result.append({
            "id": inv.id,
            "camera_id": inv.camera_id,
            "camera_name": inv.camera_name or f"相機 #{inv.camera_id}",
            "invitee_name": invitee.full_name or invitee.username or invitee.email if invitee else "未知",
            "status": inv.status,
            "created_at": inv.created_at.isoformat() if inv.created_at else None,
        })
    return result


@router.delete("/{inv_id}")
def cancel_invitation(
    inv_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("reseller", "symotus_admin")),
):
    """取消已送出的邀請（只能取消 pending 的）"""
    inv = db.query(CameraInvitation).filter(
        CameraInvitation.id == inv_id,
        CameraInvitation.inviter_id == current_user.id,
        CameraInvitation.status == "pending",
    ).first()
    if not inv:
        raise HTTPException(404, "邀請不存在或已處理")
    inv.status = "cancelled"
    db.commit()
    return {"message": "邀請已取消"}
