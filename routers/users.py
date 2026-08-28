from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db
from models import User, CameraAccess
from schemas import UserResponse, UserUpdate, ResellerUserCreate
from auth import get_current_user, require_role, hash_password
from audit import log_action

router = APIRouter(prefix="/reseller", tags=["reseller"])

# 不掛 /reseller 前綴：spec 要求 POST /users（根路徑），另建一個 router 由
# main.py 單獨掛載，避免 prefix 混進去變成 /reseller/users。
users_router = APIRouter(tags=["users"])


@users_router.post("/users", response_model=UserResponse, status_code=201)
def reseller_create_user(
    body: ResellerUserCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("reseller")),
):
    """reseller 直接建 end_user。role/reseller_id 一律強制，schema 本身也不收
    這兩個欄位，body 夾帶什麼都不影響結果。"""
    if db.query(User).filter(User.username == body.username).first():
        raise HTTPException(status_code=409, detail="username 已存在")
    if db.query(User).filter(User.email == body.email).first():
        raise HTTPException(status_code=409, detail="email 已存在")
    user = User(
        username=body.username,
        email=body.email,
        full_name=body.full_name,
        hashed_password=hash_password(body.password),
        role="end_user",
        is_active=True,
        reseller_id=current_user.id,
        created_by=current_user.id,
    )
    db.add(user)
    db.flush()
    log_action(db, current_user, "user.create", "user", user.id, "role=end_user")
    db.commit()
    db.refresh(user)
    return user

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

@router.get("/managed-users")
def managed_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("reseller", "symotus_admin"))
):
    """我管理的使用者：reseller_id 掛在我旗下，或我曾授權過相機的人（帳號管理頁 reseller 視角）"""
    granted_ids = [a.user_id for a in db.query(CameraAccess).filter(
        CameraAccess.granted_by == current_user.id,
        CameraAccess.user_id != current_user.id,
    ).all()]
    users = db.query(User).filter(
        (User.reseller_id == current_user.id) | (User.id.in_(granted_ids or [0]))
    ).all()
    return [{
        "id": u.id, "username": u.username, "full_name": u.full_name,
        "email": u.email, "role": u.role, "is_active": u.is_active,
        "line_id": u.line_id,
        "is_subordinate": u.reseller_id == current_user.id,  # true=旗下；false=僅被我授權
    } for u in sorted(users, key=lambda x: x.id)]


@router.get("/camera-access")
def my_granted_accesses(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("reseller", "symotus_admin"))
):
    """我發出的相機授權列（不含自我配對列）"""
    rows = db.query(CameraAccess).filter(
        CameraAccess.granted_by == current_user.id,
        CameraAccess.user_id != current_user.id,
    ).all()
    usernames = {u.id: u.username for u in db.query(User).all()}
    return [{
        "access_id": a.id, "camera_id": a.camera_id,
        "user_id": a.user_id, "username": usernames.get(a.user_id),
        "granted_by": a.granted_by, "granter_username": usernames.get(a.granted_by),
        "permission_level": a.permission_level or "photos_stream",
        "notify_on_online": a.notify_on_online,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    } for a in rows]


@router.patch("/camera-access/{access_id}")
def update_my_granted_access(
    access_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("reseller", "symotus_admin"))
):
    """調整我發出的授權：permission_level / notify_on_online（僅限 granted_by=自己）"""
    access = db.query(CameraAccess).filter(CameraAccess.id == access_id).first()
    if not access:
        raise HTTPException(404, "授權不存在")
    if current_user.role != "symotus_admin" and access.granted_by != current_user.id:
        raise HTTPException(403, "只有原授權者可調整此授權")
    if "permission_level" in body:
        if body["permission_level"] not in ("full", "photos_stream", "stream_only"):
            raise HTTPException(400, "permission_level 僅能是 full/photos_stream/stream_only")
        access.permission_level = body["permission_level"]
    if "notify_on_online" in body:
        access.notify_on_online = bool(body["notify_on_online"])
    log_action(db, current_user, "update_access", "camera_access", access.id,
               f"camera={access.camera_id} user={access.user_id} -> {body}")
    db.commit(); db.refresh(access)
    return {"id": access.id, "camera_id": access.camera_id, "user_id": access.user_id,
            "permission_level": access.permission_level, "notify_on_online": access.notify_on_online}


@router.post("/camera-access")
async def grant_access_to_managed_user(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("reseller", "symotus_admin"))
):
    """把「我能以 camera token 存取的相機」授權給我管理的使用者。
    相機歸屬以 Camera Backend 實際回應驗證（非 admin 需 200），受眾限旗下或已授權過的人。"""
    camera_id = body.get("camera_id")
    user_id = body.get("user_id")
    permission_level = body.get("permission_level", "photos_stream")
    if not camera_id or not user_id:
        raise HTTPException(400, "camera_id 與 user_id 必填")
    if permission_level not in ("full", "photos_stream", "stream_only"):
        raise HTTPException(400, "permission_level 僅能是 full/photos_stream/stream_only")

    # 受眾：旗下 end_user 或曾被我授權的人
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(404, "使用者不存在")
    already_granted = db.query(CameraAccess).filter(
        CameraAccess.granted_by == current_user.id,
        CameraAccess.user_id == user_id,
    ).first() is not None
    if (current_user.role != "symotus_admin"
            and target.reseller_id != current_user.id and not already_granted):
        raise HTTPException(403, "僅能授權給旗下或已授權過的使用者（其他人請用邀請連結）")

    # 相機歸屬：用自己的 camera token 實測（admin 免驗）
    if current_user.role != "symotus_admin":
        from routers.cameras import get_camera_backend_token, CAMERA_BACKEND_URL
        import httpx
        tok = await get_camera_backend_token(current_user)
        ok = False
        if tok:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(f"{CAMERA_BACKEND_URL}/api/cameras/{camera_id}",
                                     headers={"Authorization": f"Bearer {tok}"})
            ok = r.status_code == 200
        if not ok:
            # 或者：我對此相機持有他人授予的 full（轉分享）
            my_access = db.query(CameraAccess).filter(
                CameraAccess.camera_id == camera_id,
                CameraAccess.user_id == current_user.id,
                CameraAccess.permission_level == "full",
            ).first()
            if not my_access:
                raise HTTPException(403, "無此相機的授權資格")

    existing = db.query(CameraAccess).filter(
        CameraAccess.camera_id == camera_id,
        CameraAccess.user_id == user_id,
    ).first()
    if existing:
        return {"status": "already_exists", "id": existing.id}
    access = CameraAccess(camera_id=camera_id, user_id=user_id,
                          granted_by=current_user.id, permission_level=permission_level,
                          invitation_id=0)  # 非邀請來源（哨兵，避免撤銷連結時 NULL fallback 誤刪）
    db.add(access)
    log_action(db, current_user, "grant_access", "camera_access", None,
               f"camera={camera_id} user={user_id} level={permission_level}")
    db.commit(); db.refresh(access)
    return {"status": "created", "id": access.id}


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
    db.add(CameraAccess(camera_id=camera_id, user_id=user_id, granted_by=current_user.id,
                        invitation_id=0))  # 非邀請來源（哨兵，避免撤銷連結時 NULL fallback 誤刪）
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
