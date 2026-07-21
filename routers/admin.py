from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session
from database import get_db
from models import User, CameraAccess, TechSupportGrant, CameraInvitation, InviteToken
from schemas import UserResponse, TechSupportGrantResponse
from auth import require_role, decode_token
from config import settings
from datetime import datetime

router = APIRouter(prefix="/admin", tags=["admin"])

def _is_admin(x_service_key: str, authorization: str) -> bool:
    if bool(CAMERA_SERVICE_KEY) and x_service_key == CAMERA_SERVICE_KEY:
        return True
    if authorization:
        try:
            payload = decode_token(authorization.replace("Bearer ", ""))
            return payload.role == "symotus_admin"
        except Exception:
            pass
    return False

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
    """列出某用戶所有 camera_access 的相機（F-6：改用 symotus_admin JWT，移除前端明碼 service-key）
    保留 id/name/ip_address/online_status 舊欄位（舊 /admin/users 頁相容），另附授權明細。"""
    accesses = db.query(CameraAccess).filter(CameraAccess.user_id == user_id).all()
    granter_ids = {a.granted_by for a in accesses if a.granted_by}
    granters = {u.id: u.username for u in db.query(User).filter(User.id.in_(granter_ids)).all()} if granter_ids else {}
    return [{
        "id": a.camera_id, "name": None, "ip_address": None, "online_status": False,
        "access_id": a.id,
        "camera_id": a.camera_id,
        "permission_level": a.permission_level or "photos_stream",
        "granted_by": a.granted_by,
        "granter_username": granters.get(a.granted_by),
        "notify_on_online": a.notify_on_online,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    } for a in accesses]


@router.get("/support/grants", response_model=list[TechSupportGrantResponse])
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
    authorization: str = Header(None),
    db: Session = Depends(get_db),
):
    """刪除特定用戶對特定相機的存取權（service key 或 symotus_admin JWT 保護）"""
    if not _is_admin(x_service_key, authorization):
        raise HTTPException(status_code=403, detail="Invalid service key")
    deleted = db.query(CameraAccess).filter(
        CameraAccess.camera_id == camera_id,
        CameraAccess.user_id == user_id,
    ).delete()
    db.commit()
    return {"deleted": deleted, "camera_id": camera_id, "user_id": user_id}


@router.patch("/camera-access/{access_id}")
def update_camera_access(
    access_id: int,
    body: dict,
    x_service_key: str = Header(None),
    authorization: str = Header(None),
    db: Session = Depends(get_db),
):
    """更新單筆授權：permission_level / notify_on_online（service key 或 symotus_admin JWT 保護）"""
    if not _is_admin(x_service_key, authorization):
        raise HTTPException(status_code=403, detail="Invalid service key")
    access = db.query(CameraAccess).filter(CameraAccess.id == access_id).first()
    if not access:
        raise HTTPException(status_code=404, detail="camera_access not found")
    if "permission_level" in body:
        if body["permission_level"] not in ("full", "photos_stream", "stream_only"):
            raise HTTPException(status_code=400, detail="permission_level 僅能是 full/photos_stream/stream_only")
        access.permission_level = body["permission_level"]
    if "notify_on_online" in body:
        access.notify_on_online = bool(body["notify_on_online"])
    db.commit()
    db.refresh(access)
    return {"id": access.id, "camera_id": access.camera_id, "user_id": access.user_id,
            "permission_level": access.permission_level, "notify_on_online": access.notify_on_online}

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
    authorization: str = Header(None),
    db: Session = Depends(get_db),
):
    """列出所有用戶（service key 或 symotus_admin JWT 保護）"""
    if not _is_admin(x_service_key, authorization):
        raise HTTPException(status_code=403, detail="Invalid service key")
    users = db.query(User).all()
    return [{"id": u.id, "username": u.username, "email": u.email,
             "role": u.role, "line_id": u.line_id, "camera_email": u.camera_email,
             "is_active": u.is_active,
             "full_name": u.full_name, "reseller_id": u.reseller_id,
             "has_google": bool(u.google_id), "has_password": bool(u.hashed_password),
             "created_at": u.created_at.isoformat() if u.created_at else None} for u in users]


@router.get("/camera-access-all")
def list_all_camera_access(
    db: Session = Depends(get_db),
    _=Depends(require_role("symotus_admin")),
):
    """全部 camera_access 授權列（帳號總覽的授權數與授權矩陣共用資料源）"""
    rows = db.query(CameraAccess).all()
    usernames = {u.id: u.username for u in db.query(User).all()}
    return [{
        "access_id": a.id, "camera_id": a.camera_id,
        "user_id": a.user_id, "username": usernames.get(a.user_id),
        "granted_by": a.granted_by, "granter_username": usernames.get(a.granted_by),
        "permission_level": a.permission_level or "photos_stream",
        "notify_on_online": a.notify_on_online,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    } for a in rows]

@router.put("/users/{user_id}")
def update_user_admin(
    user_id: int,
    body: dict,
    x_service_key: str = Header(None),
    authorization: str = Header(None),
    db: Session = Depends(get_db),
):
    """更新用戶屬性：camera_email、role、is_active（service key 或 symotus_admin JWT 保護）"""
    if not _is_admin(x_service_key, authorization):
        raise HTTPException(status_code=403, detail="Invalid service key")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if "camera_email" in body:
        user.camera_email = body["camera_email"]
    if "camera_user_id" in body:
        user.camera_user_id = body["camera_user_id"]
    if "role" in body:
        if body["role"] not in ("symotus_admin", "reseller", "end_user"):
            raise HTTPException(status_code=400, detail="role 僅能是 symotus_admin/reseller/end_user")
        user.role = body["role"]
    if "is_active" in body:
        user.is_active = body["is_active"]
    if "reseller_id" in body:
        user.reseller_id = body["reseller_id"]  # 可為 null（解除從屬）
    db.commit()
    return {"id": user.id, "username": user.username, "email": user.email,
            "role": user.role, "camera_email": user.camera_email,
            "camera_user_id": user.camera_user_id, "is_active": user.is_active,
            "reseller_id": user.reseller_id}


@router.post("/camera-access")
def add_camera_access(
    body: dict,
    x_service_key: str = Header(None),
    authorization: str = Header(None),
    db: Session = Depends(get_db),
):
    """手動新增 camera_access 記錄（admin 用）"""
    if not _is_admin(x_service_key, authorization):
        raise HTTPException(status_code=403, detail="Invalid service key")
    camera_id = body.get("camera_id")
    user_id = body.get("user_id")
    granted_by = body.get("granted_by", user_id)
    permission_level = body.get("permission_level", "full")
    if not camera_id or not user_id:
        raise HTTPException(status_code=400, detail="camera_id and user_id required")
    existing = db.query(CameraAccess).filter(
        CameraAccess.camera_id == camera_id,
        CameraAccess.user_id == user_id,
    ).first()
    if existing:
        return {"status": "already_exists", "id": existing.id}
    access = CameraAccess(
        camera_id=camera_id,
        user_id=user_id,
        granted_by=granted_by,
        permission_level=permission_level,
    )
    db.add(access)
    db.commit()
    db.refresh(access)
    return {"status": "created", "id": access.id, "camera_id": camera_id, "user_id": user_id}

@router.get("/invitations")
def list_all_invitations(
    db: Session = Depends(get_db),
    _=Depends(require_role("symotus_admin")),
):
    """全部邀請合併檢視：相機分享邀請（camera_invitations）＋帳號註冊邀請（invite_tokens）。
    effective_status 對 pending 且已過期者標記 expired（資料庫 status 不動）。"""
    now = datetime.utcnow()
    usernames = {u.id: (u.full_name or u.username) for u in db.query(User).all()}

    def eff(status, expires_at):
        if status == "pending" and expires_at and expires_at < now:
            return "expired"
        return status

    cam_invs = db.query(CameraInvitation).order_by(CameraInvitation.created_at.desc()).all()
    tokens = db.query(InviteToken).order_by(InviteToken.created_at.desc()).all()
    return {
        "camera_invitations": [{
            "id": i.id, "token": i.token, "camera_id": i.camera_id, "camera_name": i.camera_name,
            "inviter_id": i.inviter_id, "inviter_name": usernames.get(i.inviter_id),
            "invitee_id": i.invitee_id, "invitee_name": usernames.get(i.invitee_id),
            "permission_level": i.permission_level, "is_public": i.is_public,
            "status": i.status, "effective_status": eff(i.status, i.expires_at),
            "invite_url": f"{settings.FRONTEND_URL}/camera-invite/{i.token}",
            "expires_at": i.expires_at.isoformat() if i.expires_at else None,
            "created_at": i.created_at.isoformat() if i.created_at else None,
        } for i in cam_invs],
        "invite_tokens": [{
            "id": t.id, "token": t.token,
            "inviter_id": t.reseller_id, "inviter_name": usernames.get(t.reseller_id),
            "email": t.email, "intended_role": t.intended_role, "camera_ids": t.camera_ids,
            "status": t.status, "effective_status": eff(t.status, t.expires_at),
            "accepted_by": t.accepted_by, "accepted_by_name": usernames.get(t.accepted_by),
            "invite_url": f"{settings.FRONTEND_URL}/invite/{t.token}",
            "expires_at": t.expires_at.isoformat() if t.expires_at else None,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        } for t in tokens],
    }


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
