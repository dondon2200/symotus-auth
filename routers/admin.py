from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from database import get_db
from models import User, TechSupportGrant
from schemas import UserResponse
from auth import require_role
from datetime import datetime

router = APIRouter(prefix="/admin", tags=["admin"])

@router.get("/resellers", response_model=list[UserResponse])
def list_resellers(
    db: Session = Depends(get_db),
    _=Depends(require_role("symotus_admin"))
):
    return db.query(User).filter(User.role == "reseller").all()

@router.get("/resellers/{reseller_id}/users", response_model=list[UserResponse])
def reseller_users(
    reseller_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_role("symotus_admin"))
):
    return db.query(User).filter(User.reseller_id == reseller_id).all()

@router.get("/support/grants")
def all_grants(
    db: Session = Depends(get_db),
    _=Depends(require_role("symotus_admin"))
):
    return db.query(TechSupportGrant).filter(
        TechSupportGrant.expires_at > datetime.utcnow(),
        TechSupportGrant.revoked_at == None
    ).all()
