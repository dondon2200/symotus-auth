from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session
from database import get_db
from models import User, CameraAccess, TechSupportGrant
from schemas import UserResponse
from auth import require_role
from datetime import datetime

router = APIRouter(prefix="/admin", tags=["admin"])

@router.get("/resellers")
def list_resellers(
    db: Session = Depends(get_db),
    _=Depends(require_role("symotus_admin"))
):
    resellers = db.query(User).filter(User.role == "reseller").all()
    result = []
    for u in resellers:
        # 相機數 = camera_access（自己配對或被授權）
        cam_count = db.query(CameraAccess).filter(CameraAccess.user_id == u.id).count()
        result.append({
            "id": u.id,
            "username": u.username,
            "full_name": u.full_name,
            "email": u.email,
            "role": u.role,
            "line_id": u.line_id,
            "camera_email": u.camera_email,
            "is_active": u.is_active,
            "camera_count": cam_count,
        })
    return result

@router.get("/resellers/{reseller_id}/users", response_model=list[UserResponse])
def reseller_users(
    reseller_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_role("symotus_admin"))
):
    return db.query(User).filter(User.reseller_id == reseller_id).all()

@router.get("/camera-access-by-user/{user_id}")
def camera_access_by_user(
    user_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_role("symotus_admin")),
):
    """列出某用戶所有 camera_access 的相機（F-6：改用 symotus_admin JWT，移除前端明碼 service-key）"""
    accesses = db.query(CameraAccess).filter(CameraAccess.user_id == user_id).all()
    return [{"id": a.camera_id, "name": None, "ip_address": None, "online_status": False} for a in accesses]


@router.get("/support/grants")
def all_grants(
    db: Session = Depends(get_db),
    _=Depends(require_role("symotus_admin"))
):
    return db.query(TechSupportGrant).filter(
        TechSupportGrant.expires_at > datetime.utcnow(),
        TechSupportGrant.revoked_at == None
    ).all()


import os
CAMERA_SERVICE_KEY = os.environ.get("CAMERA_SERVICE_KEY", "")

@router.delete("/camera-access")
def remove_camera_access(
    camera_id: int,
    user_id: int,
    x_service_key: str = Header(None),
    db: Session = Depends(get_db),
):
    """用 service key 刪除特定用戶對特定相機的存取權"""
    if x_service_key != CAMERA_SERVICE_KEY:
        raise HTTPException(status_code=403, detail="Invalid service key")
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
        raise HTTPException(status_code=403, detail="Invalid service key")
    rows = db.query(CameraAccess).filter(CameraAccess.camera_id == camera_id).all()
    users = []
    for r in rows:
        u = db.query(User).filter(User.id == r.user_id).first()
        users.append({"access_id": r.id, "user_id": r.user_id,
                      "email": u.email if u else None,
                      "username": u.username if u else None})
    return {"camera_id": camera_id, "users": users}

@router.get("/users")
def list_all_users(
    x_service_key: str = Header(None),
    db: Session = Depends(get_db),
):
    """列出所有用戶（service key 保護）"""
    if x_service_key != CAMERA_SERVICE_KEY:
        raise HTTPException(status_code=403, detail="Invalid service key")
    users = db.query(User).all()
    return [{"id": u.id, "username": u.username, "email": u.email,
             "role": u.role, "line_id": u.line_id, "camera_email": u.camera_email,
             "is_active": u.is_active} for u in users]

@router.put("/users/{user_id}")
def update_user_admin(
    user_id: int,
    body: dict,
    x_service_key: str = Header(None),
    db: Session = Depends(get_db),
):
    """更新用戶屬性：camera_email、role、is_active（service key 保護）"""
    if x_service_key != CAMERA_SERVICE_KEY:
        raise HTTPException(status_code=403, detail="Invalid service key")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if "camera_email" in body:
        user.camera_email = body["camera_email"]
    if "camera_user_id" in body:
        user.camera_user_id = body["camera_user_id"]
    if "role" in body:
        user.role = body["role"]
    if "is_active" in body:
        user.is_active = body["is_active"]
    db.commit()
    return {"id": user.id, "username": user.username, "email": user.email,
            "role": user.role, "camera_email": user.camera_email,
            "camera_user_id": user.camera_user_id, "is_active": user.is_active}

@router.post("/migrate/add-camera-user-id")
def migrate_add_camera_user_id(
    x_service_key: str = Header(None),
    db: Session = Depends(get_db),
):
    """一次性 migration：加 camera_user_id 欄位並設定 admin@timelapse.com"""
    if x_service_key != CAMERA_SERVICE_KEY:
        raise HTTPException(status_code=403, detail="Invalid service key")
    from sqlalchemy import text
    try:
        db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS camera_user_id INTEGER"))
        db.execute(text("UPDATE users SET camera_user_id = 1 WHERE email = 'admin@timelapse.com'"))
        db.commit()
        # 查結果
        result = db.execute(text("SELECT id, email, camera_user_id FROM users")).fetchall()
        return {"ok": True, "users": [{"id": r[0], "email": r[1], "camera_user_id": r[2]} for r in result]}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/migrate/fix-camera-invitations")
def fix_camera_invitations(db: Session = Depends(get_db), service_key: str = Header(None, alias="x-service-key")):
    """一次性：補上 camera_invitations 和 camera_access 缺少的欄位"""
    if service_key != CAMERA_SERVICE_KEY:
        raise HTTPException(403, "Forbidden")
    from sqlalchemy import text
    results = []
    sqls = [
        "ALTER TABLE camera_invitations ADD COLUMN IF NOT EXISTS token VARCHAR",
        "ALTER TABLE camera_invitations ADD COLUMN IF NOT EXISTS permission_level VARCHAR DEFAULT 'photos_stream'",
        "ALTER TABLE camera_invitations ADD COLUMN IF NOT EXISTS invitee_id INTEGER",
        "ALTER TABLE camera_invitations ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP",
        "ALTER TABLE camera_invitations ADD COLUMN IF NOT EXISTS responded_at TIMESTAMP",
        "ALTER TABLE camera_access ADD COLUMN IF NOT EXISTS permission_level VARCHAR DEFAULT 'photos_stream'",
        "ALTER TABLE camera_invitations ALTER COLUMN invitee_id DROP NOT NULL",
        "ALTER TABLE camera_invitations ALTER COLUMN inviter_id DROP NOT NULL",
    ]
    for sql in sqls:
        try:
            db.execute(text(sql))
            db.commit()
            results.append({"sql": sql, "ok": True})
        except Exception as e:
            db.rollback()
            results.append({"sql": sql, "ok": False, "err": str(e)[:100]})
    return {"results": results}
