from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db
from models import User, CameraAccess
from schemas import UserResponse, UserUpdate
from auth import get_current_user, require_role
from audit import log_action

router = APIRouter(prefix="/reseller", tags=["reseller"])

@router.get("/users", response_model=list[UserResponse])
def list_end_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("reseller", "symotus_admin"))
):
    return db.query(User).filter(
        User.reseller_id == current_user.id,
        User.role == "end_user"
    ).all()

@router.put("/users/{user_id}", response_model=UserResponse)
def update_end_user(
    user_id: int,
    body: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("reseller", "symotus_admin"))
):
    user = db.query(User).filter(
        User.id == user_id,
        User.reseller_id == current_user.id
    ).first()
    if not user:
        raise HTTPException(404, "使用者不存在")
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.full_name is not None:
        user.full_name = body.full_name
    db.commit(); db.refresh(user)
    return user

@router.delete("/users/{user_id}")
def remove_end_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("reseller", "symotus_admin"))
):
    user = db.query(User).filter(
        User.id == user_id,
        User.reseller_id == current_user.id
    ).first()
    if not user:
        raise HTTPException(404, "使用者不存在")
    # 解除綁定而不是刪除帳號
    user.reseller_id = None; user.is_active = False
    db.query(CameraAccess).filter(CameraAccess.user_id == user_id).delete()
    db.commit()
    return {"message": "使用者已移除"}

@router.get("/cameras/{camera_id}/access")
def list_camera_access(
    camera_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("reseller", "symotus_admin"))
):
    accesses = db.query(CameraAccess).filter(CameraAccess.camera_id == camera_id).all()
    return accesses

@router.post("/cameras/{camera_id}/access")
def grant_camera_access(
    camera_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("reseller", "symotus_admin"))
):
    user_id = body.get("user_id")
    user = db.query(User).filter(User.id == user_id, User.reseller_id == current_user.id).first()
    if not user:
        raise HTTPException(404, "使用者不存在或不屬於你")
    existing = db.query(CameraAccess).filter(
        CameraAccess.camera_id == camera_id,
        CameraAccess.user_id == user_id
    ).first()
    if existing:
        return {"message": "已有存取權限"}
    db.add(CameraAccess(camera_id=camera_id, user_id=user_id, granted_by=current_user.id))
    log_action(db, current_user, "grant_access", "camera_access", None,
               f"camera={camera_id} user={user_id}")
    db.commit()
    return {"message": "已分配相機存取權"}

@router.delete("/cameras/{camera_id}/access/{user_id}")
def revoke_camera_access(
    camera_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("reseller", "symotus_admin"))
):
    access = db.query(CameraAccess).filter(
        CameraAccess.camera_id == camera_id,
        CameraAccess.user_id == user_id
    ).first()
    if not access:
        raise HTTPException(404, "存取權限不存在")
    # ownership：只有原授權者或平台管理員可撤銷，防止任意 reseller 撤他人授權
    if current_user.role != "symotus_admin" and access.granted_by != current_user.id:
        raise HTTPException(403, "只有原授權者可撤銷此存取權限")
    db.delete(access)
    log_action(db, current_user, "revoke_access", "camera_access", access.id,
               f"camera={camera_id} user={user_id}")
    db.commit()
    return {"message": "已撤銷存取權限"}
