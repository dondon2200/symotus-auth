from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime, timedelta

from database import get_db
from models import User, InviteToken
from audit import log_action
from schemas import InviteCreate, InviteResponse, InvitePreview
from auth import get_current_user
from role_policies import require_feature, role_allows
from config import settings

router = APIRouter(prefix="/invites", tags=["invites"])

@router.post("", response_model=InviteResponse)
def create_invite(
    body: InviteCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_feature("account.invite"))
):
    # 角色政策 account.invite_reseller（預設只有 symotus_admin）才能發「reseller」邀請；其餘一律 end_user。
    intended_role = "end_user"
    if body.intended_role == "reseller":
        if not role_allows(db, "account.invite_reseller", current_user):
            raise HTTPException(403, "此角色沒有「發 reseller 邀請」的權限")
        intended_role = "reseller"
    elif body.intended_role not in ("end_user", None, ""):
        raise HTTPException(400, "intended_role 僅能是 end_user 或 reseller")

    invite = InviteToken(
        reseller_id=current_user.id,
        camera_ids=body.camera_ids,
        email=body.email,
        intended_role=intended_role,
        expires_at=datetime.utcnow() + timedelta(hours=body.expires_hours),
    )
    db.add(invite); db.commit(); db.refresh(invite)
    invite_url = f"{settings.FRONTEND_URL}/invite/{invite.token}"
    return {**invite.__dict__, "invite_url": invite_url}

@router.get("", response_model=list[InviteResponse])
def list_invites(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_feature("account.invite"))
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
    current_user: User = Depends(require_feature("account.invite"))
):
    q = db.query(InviteToken).filter(
        InviteToken.token == token,
        InviteToken.status == "pending",
    )
    # 角色政策 invitation.revoke_any（預設只有 admin）可代撤任何人發的；否則只能撤自己發的
    if not role_allows(db, "invitation.revoke_any", current_user):
        q = q.filter(InviteToken.reseller_id == current_user.id)
    invite = q.first()
    if not invite:
        raise HTTPException(404, "邀請不存在或已無法撤銷")
    invite.status = "revoked"
    log_action(db, current_user, "revoke_invite_token", "invite_token", invite.id,
               f"email={invite.email} intended_role={invite.intended_role}")
    db.commit()
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
