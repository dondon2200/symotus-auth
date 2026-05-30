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

from fastapi import Header, HTTPException as HTTPEx
from models import CameraAccess

CAMERA_SERVICE_KEY = "9ad3343a32508c209152a450f601b990176fa4d41c94c27330e448b1a86826c2"

@router.delete("/camera-access")
def remove_camera_access(
    camera_id: int,
    user_id: int,
    x_service_key: str = Header(None),
    db: Session = Depends(get_db),
):
    """用 service key 刪除特定用戶對特定相機的存取權"""
    if x_service_key != CAMERA_SERVICE_KEY:
        raise HTTPEx(status_code=403, detail="Invalid service key")
    deleted = db.query(CameraAccess).filter(
        CameraAccess.camera_id == camera_id,
        CameraAccess.user_id == user_id,
    ).delete()
    db.commit()
    return {"deleted": deleted, "camera_id": camera_id, "user_id": user_id}

@router.get("/camera-access/{camera_id}")
def get_camera_access(
    camera_id: int,
    x_service_key: str = Header(None),
    db: Session = Depends(get_db),
):
    """列出特定相機的所有存取用戶"""
    if x_service_key != CAMERA_SERVICE_KEY:
        raise HTTPEx(status_code=403, detail="Invalid service key")
    rows = db.query(CameraAccess).filter(CameraAccess.camera_id == camera_id).all()
    users = []
    for r in rows:
        u = db.query(User).filter(User.id == r.user_id).first()
        users.append({"access_id": r.id, "user_id": r.user_id,
                      "email": u.email if u else None,
                      "username": u.username if u else None})
    return {"camera_id": camera_id, "users": users}
