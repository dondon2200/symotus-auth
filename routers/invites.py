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
    invite = InviteToken(
        reseller_id=current_user.id,
        camera_ids=body.camera_ids,
        email=body.email,
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
    invite = db.query(InviteToken).filter(
        InviteToken.token == token,
        InviteToken.reseller_id == current_user.id,
        InviteToken.status == "pending",
    ).first()
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
