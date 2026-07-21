from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime, timedelta

from database import get_db
from models import User, InviteToken
from schemas import InviteCreate, InviteResponse, InvitePreview
from auth import get_current_user, require_role
from config import settings

router = APIRouter(prefix="/invites", tags=["invites"])

@router.post("", response_model=InviteResponse)
def create_invite(
    body: InviteCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("reseller", "symotus_admin"))
):
    # 角色把關：只有 symotus_admin 能發「reseller」邀請；其餘一律 end_user。
    # camera_email/camera_user_id 僅在 reseller 邀請（且由 admin 發）時預綁。
    intended_role = "end_user"
    camera_email = None
    camera_user_id = None
    if body.intended_role == "reseller":
        if current_user.role != "symotus_admin":
            raise HTTPException(403, "只有平台管理員能發出 reseller 邀請")
        intended_role = "reseller"
        camera_email = body.camera_email
        camera_user_id = body.camera_user_id
    elif body.intended_role not in ("end_user", None, ""):
        raise HTTPException(400, "intended_role 僅能是 end_user 或 reseller")

    invite = InviteToken(
        reseller_id=current_user.id,
        camera_ids=body.camera_ids,
        email=body.email,
        intended_role=intended_role,
        camera_email=camera_email,
        camera_user_id=camera_user_id,
        expires_at=datetime.utcnow() + timedelta(hours=body.expires_hours),
    )
    db.add(invite); db.commit(); db.refresh(invite)
    invite_url = f"{settings.FRONTEND_URL}/invite/{invite.token}"
    return {**invite.__dict__, "invite_url": invite_url}

@router.get("", response_model=list[InviteResponse])
def list_invites(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("reseller", "symotus_admin"))
):
    # 過期的自動標記
    db.query(InviteToken).filter(
        InviteToken.reseller_id == current_user.id,
        InviteToken.status == "pending",
        InviteToken.expires_at < datetime.utcnow()
    ).update({"status": "expired"})
    db.commit()
    invites = db.query(InviteToken).filter(InviteToken.reseller_id == current_user.id).all()
    return [{**i.__dict__, "invite_url": f"{settings.FRONTEND_URL}/invite/{i.token}"} for i in invites]

@router.delete("/{token}")
def revoke_invite(
    token: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("reseller", "symotus_admin"))
):
    q = db.query(InviteToken).filter(
        InviteToken.token == token,
        InviteToken.status == "pending",
    )
    # symotus_admin 可代撤任何人發的邀請；其他角色僅能撤自己發的
    if current_user.role != "symotus_admin":
        q = q.filter(InviteToken.reseller_id == current_user.id)
    invite = q.first()
    if not invite:
        raise HTTPException(404, "邀請不存在或已無法撤銷")
    invite.status = "revoked"; db.commit()
    return {"message": "邀請已撤銷"}

@router.get("/preview/{token}", response_model=InvitePreview)
def preview_invite(token: str, db: Session = Depends(get_db)):
    """公開 endpoint，不需要登入，供邀請頁面顯示邀請資訊"""
    invite = db.query(InviteToken).filter(InviteToken.token == token).first()
    if not invite:
        return InvitePreview(valid=False, reason="not_found")
    if invite.status == "revoked":
        return InvitePreview(valid=False, reason="revoked")
    if invite.status == "accepted":
        return InvitePreview(valid=False, reason="already_used")
    if invite.expires_at < datetime.utcnow():
        return InvitePreview(valid=False, reason="expired")
    reseller = db.query(User).filter(User.id == invite.reseller_id).first()
    camera_count = len(invite.camera_ids) if invite.camera_ids else 0
    return InvitePreview(
        valid=True,
        reseller_name=reseller.full_name or reseller.username,
        camera_count=camera_count,
        expires_at=invite.expires_at,
    )
