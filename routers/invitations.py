"""
相機邀請系統（連結式）
Admin/Reseller 產生邀請連結 → 分享給任何人 → 點連結接受
"""
import secrets
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, and_
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from pydantic import BaseModel
from typing import Optional

from database import get_db
from models import User, CameraInvitation, CameraAccess
from auth import get_current_user, require_role
from audit import log_action
from config import settings
from schemas import utc_iso

router = APIRouter(prefix="/invitations", tags=["invitations"])

FRONTEND_URL = getattr(settings, "FRONTEND_URL", "https://user.symotus.com")


PERMISSION_LABELS = {
    "full": "全功能管理（設定、控制、下載、可再分享）",
    "photos_stream": "縮時與下載（照片、產縮時、下載原檔）",
    "stream_only": "僅預覽觀看（縮時預覽＋相簿檢視，不可下載）",
}

class CreateInvitationBody(BaseModel):
    camera_id: int
    camera_name: Optional[str] = None
    note: Optional[str] = None
    permission_level: str = "photos_stream"  # full / photos_stream / stream_only
    expires_hours: Optional[int] = None  # None = 不過期
    is_public: bool = False  # 公開連結，不需登入


# ── 建立邀請（產生連結）────────────────────────────────────────────────────────
@router.post("")
def create_invitation(
    body: CreateInvitationBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("reseller", "symotus_admin")),
):
    if body.permission_level not in ("full", "photos_stream", "stream_only"):
        raise HTTPException(400, "未知的權限模式")
    if body.is_public and body.permission_level != "stream_only":
        raise HTTPException(400, "公開連結僅適用「僅預覽觀看」模式")

    # 防重複：同相機同模式已有仍有效的連結（pending/accepted 皆算，連結可重複接受）→ 回傳現有連結
    existing_inv = db.query(CameraInvitation).filter(
        CameraInvitation.inviter_id == current_user.id,
        CameraInvitation.camera_id == body.camera_id,
        CameraInvitation.permission_level == body.permission_level,  # D1：一模式一連結
        CameraInvitation.status.in_(["pending", "accepted"]),
        CameraInvitation.is_public == body.is_public,
    ).first()
    if existing_inv:
        invite_url = f"{FRONTEND_URL}/camera-invite/{existing_inv.token}"
        return {
            "id": existing_inv.id, "token": existing_inv.token,
            "invite_url": invite_url, "camera_name": existing_inv.camera_name,
            "expires_at": utc_iso(existing_inv.expires_at),
            "reused": True,
        }

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
        permission_level=body.permission_level,
        is_public=body.is_public,
        expires_at=expires_at,
    )
    db.add(inv); db.commit(); db.refresh(inv)

    invite_url = f"{FRONTEND_URL}/camera-invite/{token}"
    return {
        "id": inv.id,
        "token": token,
        "invite_url": invite_url,
        "camera_name": inv.camera_name,
        "expires_at": utc_iso(expires_at),
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
            "created_at": utc_iso(inv.created_at),
        })
    return result


@router.get("/preview/{token}")
def preview_invitation(token: str, db: Session = Depends(get_db)):
    inv = db.query(CameraInvitation).filter(CameraInvitation.token == token).first()
    if not inv:
        return {"valid": False, "reason": "not_found"}
    # 連結可重複接受：只有撤銷或過期才失效（accepted/declined 不擋其他人使用同一連結）
    if inv.status == "revoked":
        return {"valid": False, "reason": "revoked"}
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
        "expires_at": utc_iso(inv.expires_at),
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
    # 連結可重複接受：撤銷或過期前，多位使用者可用同一連結取得存取權
    if inv.status == "revoked":
        raise HTTPException(400, "此邀請已撤銷")
    if inv.expires_at and inv.expires_at < datetime.utcnow():
        raise HTTPException(400, "邀請連結已過期")

    # 若已有此相機的 camera_access：定案②以最新接受為準（可升可降），同步更新來源連結與分享者
    existing = db.query(CameraAccess).filter(
        CameraAccess.user_id == current_user.id,
        CameraAccess.camera_id == inv.camera_id,
    ).first()
    if existing:
        existing.permission_level = inv.permission_level
        existing.granted_by = inv.inviter_id
        existing.invitation_id = inv.id
    else:
        db.add(CameraAccess(camera_id=inv.camera_id, user_id=current_user.id,
                             granted_by=inv.inviter_id,
                             permission_level=inv.permission_level,
                             invitation_id=inv.id))

    # status 標記為 accepted 僅供分享者檢視（不會使連結失效）；invitee_id 記錄最近一位接受者
    inv.status = "accepted"
    inv.invitee_id = current_user.id
    inv.responded_at = datetime.utcnow()
    # 從屬樹回填：end_user 首次接受某 reseller/admin 的分享 → 掛到該邀請者旗下
    if current_user.role == "end_user" and not current_user.reseller_id and inv.inviter_id:
        inviter = db.query(User).filter(User.id == inv.inviter_id).first()
        if inviter and inviter.role in ("reseller", "symotus_admin"):
            current_user.reseller_id = inviter.id
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
    if inv.status == "revoked":
        raise HTTPException(400, "此邀請已撤銷")

    # 連結可重複使用：單一使用者拒絕不改變連結效力，僅記錄回應
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
            "created_at": utc_iso(inv.created_at),
            "permission_level": inv.permission_level,
            "permission_label": PERMISSION_LABELS.get(inv.permission_level, ""),
            "invitee_name": (invitee.full_name or invitee.username or invitee.email) if invitee else None,
            "expires_at": utc_iso(inv.expires_at),
        })
    return result


# ── 取消邀請──────────────────────────────────────────────────────────────────
@router.delete("/{inv_id}")
def cancel_invitation(
    inv_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("reseller", "symotus_admin")),
):
    q = db.query(CameraInvitation).filter(
        CameraInvitation.id == inv_id,
        CameraInvitation.status.in_(["pending", "accepted"]),
    )
    # symotus_admin 可代撤任何人發的邀請；其他角色僅能撤自己發的
    if current_user.role != "symotus_admin":
        q = q.filter(CameraInvitation.inviter_id == current_user.id)
    inv = q.first()
    if not inv:
        raise HTTPException(404, "邀請不存在或無法撤銷")
    # D2：只移除由本連結建立的授權；舊資料（invitation_id 為 NULL）以
    # granted_by＋permission_level fallback，避免誤刪同相機其他模式的授權
    db.query(CameraAccess).filter(
        CameraAccess.camera_id == inv.camera_id,
        or_(
            CameraAccess.invitation_id == inv.id,
            and_(CameraAccess.invitation_id.is_(None),
                 CameraAccess.granted_by == inv.inviter_id,
                 CameraAccess.permission_level == inv.permission_level),
        ),
    ).delete(synchronize_session=False)
    inv.status = "revoked"
    log_action(db, current_user, "revoke_invitation", "invitation", inv.id,
               f"camera={inv.camera_id} inviter={inv.inviter_id} invitee={inv.invitee_id}")
    db.commit()
    return {"message": "已停止分享"}
