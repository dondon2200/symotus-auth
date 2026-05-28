from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from database import get_db
from models import User, TechSupportGrant
from schemas import TechSupportGrantCreate, TechSupportGrantResponse
from auth import get_current_user, require_role

router = APIRouter(prefix="/support", tags=["support"])

@router.post("/grants", response_model=TechSupportGrantResponse)
def create_grant(
    body: TechSupportGrantCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("reseller"))
):
    grant = TechSupportGrant(
        reseller_id=current_user.id,
        granted_by=current_user.id,
        camera_ids=body.camera_ids,
        expires_at=datetime.utcnow() + timedelta(hours=body.duration_hours),
    )
    db.add(grant); db.commit(); db.refresh(grant)
    return grant

@router.get("/grants", response_model=list[TechSupportGrantResponse])
def list_grants(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role == "reseller":
        return db.query(TechSupportGrant).filter(
            TechSupportGrant.reseller_id == current_user.id,
            TechSupportGrant.expires_at > datetime.utcnow(),
            TechSupportGrant.revoked_at == None
        ).all()
    if current_user.role == "symotus_admin":
        return db.query(TechSupportGrant).filter(
            TechSupportGrant.expires_at > datetime.utcnow(),
            TechSupportGrant.revoked_at == None
        ).all()
    raise HTTPException(403, "No permission")

@router.delete("/grants/{grant_id}")
def revoke_grant(
    grant_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("reseller"))
):
    grant = db.query(TechSupportGrant).filter(
        TechSupportGrant.id == grant_id,
        TechSupportGrant.reseller_id == current_user.id
    ).first()
    if not grant:
        raise HTTPException(404, "授權不存在")
    grant.revoked_at = datetime.utcnow(); db.commit()
    return {"message": "授權已撤銷"}
