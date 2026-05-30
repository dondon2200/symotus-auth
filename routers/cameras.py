"""
Cameras proxy router - Auth Service 管理相機存取權限
所有相機 API 都經過這裡，Auth Service 負責權限控制
Camera Backend 不管權限，只負責相機操作
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from typing import Optional
import httpx

from database import get_db
from models import User, CameraAccess
from auth import get_current_user

router = APIRouter(prefix="/cameras", tags=["cameras"])

CAMERA_BACKEND_URL = "https://user.symotus.com"
CAMERA_SERVICE_KEY = "9ad3343a32508c209152a450f601b990176fa4d41c94c27330e448b1a86826c2"


async def get_camera_backend_token(user: User) -> str:
    """取得 Camera Backend token（用 service key，不依賴 user 的 camera_email）"""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{CAMERA_BACKEND_URL}/internal/auth/token",
            headers={"x-service-key": CAMERA_SERVICE_KEY},
            json={"user_id": user.id, "email": user.camera_email or f"line_{user.id}@symotus.com", "role": user.role},
        )
        if resp.status_code == 200:
            return resp.json().get("access_token", "")
    return ""


def get_allowed_camera_ids(user: User, db: Session) -> Optional[list[int]]:
    """
    取得用戶可存取的 camera_id 列表
    - reseller/symotus_admin: None (不限制，Camera Backend 自己管)
    - end_user: 只能看 camera_access 表裡授權的相機
    """
    if user.role in ("reseller", "symotus_admin"):
        return None  # 不限制
    # end_user 只能看被授權的相機
    accesses = db.query(CameraAccess).filter(CameraAccess.user_id == user.id).all()
    return [a.camera_id for a in accesses]


@router.get("")
async def list_cameras(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """取得用戶可存取的相機列表（Auth Service 控制權限）"""
    allowed_ids = get_allowed_camera_ids(current_user, db)
    cam_token = await get_camera_backend_token(current_user)
    if not cam_token:
        raise HTTPException(502, "無法取得 Camera Backend token")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{CAMERA_BACKEND_URL}/api/cameras",
            headers={"Authorization": f"Bearer {cam_token}"},
        )
        if resp.status_code != 200:
            raise HTTPException(resp.status_code, "Camera Backend 錯誤")
        data = resp.json()

    cameras = data.get("cameras", [])

    # end_user 只過濾有權限的相機
    if allowed_ids is not None:
        cameras = [c for c in cameras if c["id"] in allowed_ids]

    return {"cameras": cameras, "total": len(cameras)}


@router.get("/thumbnails/latest")
async def get_thumbnails(
    ids: str = "",
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """取得相機縮圖（驗證每個 id 的存取權限）"""
    allowed_ids = get_allowed_camera_ids(current_user, db)
    requested_ids = [int(i) for i in ids.split(",") if i.strip().isdigit()]

    # 過濾掉沒有權限的 id
    if allowed_ids is not None:
        requested_ids = [i for i in requested_ids if i in allowed_ids]

    if not requested_ids:
        return {}

    cam_token = await get_camera_backend_token(current_user)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{CAMERA_BACKEND_URL}/api/cameras/thumbnails/latest",
            headers={"Authorization": f"Bearer {cam_token}"},
            params={"ids": ",".join(str(i) for i in requested_ids)},
        )
        if resp.status_code == 200:
            return resp.json()
    return {}


@router.get("/{camera_id}")
async def get_camera(
    camera_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """取得單台相機詳情（驗證權限）"""
    allowed_ids = get_allowed_camera_ids(current_user, db)
    if allowed_ids is not None and camera_id not in allowed_ids:
        raise HTTPException(403, "無此相機的存取權限")

    cam_token = await get_camera_backend_token(current_user)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{CAMERA_BACKEND_URL}/api/cameras/{camera_id}",
            headers={"Authorization": f"Bearer {cam_token}"},
        )
        if resp.status_code == 200:
            return resp.json()
        raise HTTPException(resp.status_code, resp.text)


@router.post("/{camera_id}/unbind")
async def unbind_camera(
    camera_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    解除相機綁定：
    - reseller: 用自己的 camera token 呼叫 Camera Backend unbind
    - end_user: 只撤銷 camera_access 記錄，不動 Camera Backend
    """
    allowed_ids = get_allowed_camera_ids(current_user, db)
    if allowed_ids is not None and camera_id not in allowed_ids:
        raise HTTPException(403, "無此相機的存取權限")

    if current_user.role == "end_user":
        # end_user 只刪 camera_access 記錄
        deleted = db.query(CameraAccess).filter(
            CameraAccess.camera_id == camera_id,
            CameraAccess.user_id == current_user.id,
        ).delete()
        db.commit()
        if deleted:
            return {"success": True, "message": "已移除相機存取權限"}
        raise HTTPException(404, "存取權限不存在")

    # reseller: 呼叫 Camera Backend unbind
    cam_token = await get_camera_backend_token(current_user)
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{CAMERA_BACKEND_URL}/api/cameras/{camera_id}/unbind",
            headers={"Authorization": f"Bearer {cam_token}", "Content-Type": "application/json"},
            json={},
        )
        if resp.status_code == 200:
            return resp.json()
        raise HTTPException(resp.status_code, resp.text)


@router.api_route("/{camera_id}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_camera_api(
    camera_id: int,
    path: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    通用 proxy：所有其他相機 API（設定、排程等）
    先驗證權限，再轉發到 Camera Backend
    """
    allowed_ids = get_allowed_camera_ids(current_user, db)
    if allowed_ids is not None and camera_id not in allowed_ids:
        raise HTTPException(403, "無此相機的存取權限")

    cam_token = await get_camera_backend_token(current_user)
    body = await request.body()
    headers = {"Authorization": f"Bearer {cam_token}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.request(
            method=request.method,
            url=f"{CAMERA_BACKEND_URL}/api/cameras/{camera_id}/{path}",
            headers=headers,
            content=body,
            params=dict(request.query_params),
        )
        return JSONResponse(status_code=resp.status_code, content=resp.json() if resp.content else {})
