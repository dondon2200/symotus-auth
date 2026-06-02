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
    """取得 Camera Backend token
    安全原則：
    - 必須有 camera_email 才能換 token（代表該帳號有在 Camera Backend 配對過相機）
    - 用 camera_user_id（Camera Backend 的真實 user id）+ camera_email 換 token
    - 沒有 camera_email 的用戶無法直接存取 Camera Backend，只能透過 camera_access 看授權相機
    """
    if not user.camera_email:
        return ""  # 沒有 camera_email = 沒有 Camera Backend 帳號，不給 token
    # user_id=0 讓 Camera Backend 純用 email 查帳號，避免 user_id 不一致問題
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{CAMERA_BACKEND_URL}/internal/auth/token",
            headers={"x-service-key": CAMERA_SERVICE_KEY},
            json={"user_id": 0, "email": user.camera_email, "role": user.role},
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

    if cam_token:
        # 有 camera token：直接從 Camera Backend 拿自己的相機列表
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{CAMERA_BACKEND_URL}/api/cameras",
                headers={"Authorization": f"Bearer {cam_token}"},
            )
            if resp.status_code != 200:
                raise HTTPException(resp.status_code, "Camera Backend 錯誤")
            data = resp.json()
        cameras = data.get("cameras", [])
        # reseller 看自己的相機；如果有 allowed_ids 限制再過濾
        if allowed_ids is not None:
            cameras = [c for c in cameras if c["id"] in allowed_ids]
    else:
        # 沒有 camera token（end_user 或未配對用戶）：
        # 只能看 camera_access 授權的相機，需要用 owner 的 token 去拿資料
        if not allowed_ids:
            return {"cameras": [], "total": 0}
        cameras = []
        # 找各台相機的 owner，用 owner token 拿資料
        for cam_id in allowed_ids:
            # 找誰 granted 這個 camera_access（granted_by = reseller/owner）
            access = db.query(CameraAccess).filter(CameraAccess.camera_id == cam_id,
                                                    CameraAccess.user_id == current_user.id).first()
            if not access:
                continue
            owner = db.query(User).filter(User.id == access.granted_by).first()
            if not owner or not owner.camera_email:
                continue
            owner_token = await get_camera_backend_token(owner)
            if not owner_token:
                continue
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"{CAMERA_BACKEND_URL}/api/cameras/{cam_id}",
                                     headers={"Authorization": f"Bearer {owner_token}"})
                if r.status_code == 200:
                    cameras.append(r.json())

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


@router.post("")
async def create_camera(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """新增/配對相機 — 只有 reseller 和 symotus_admin 可以新增相機"""
    if current_user.role not in ("reseller", "symotus_admin"):
        raise HTTPException(403, "只有 reseller 或 admin 可以新增相機")
    cam_token = await get_camera_backend_token(current_user)
    if not cam_token:
        raise HTTPException(502, "無法取得 Camera Backend token")
    body = await request.body()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{CAMERA_BACKEND_URL}/api/cameras",
            headers={"Authorization": f"Bearer {cam_token}", "Content-Type": "application/json"},
            content=body,
        )
        try:
            return JSONResponse(status_code=resp.status_code, content=resp.json())
        except Exception:
            return JSONResponse(status_code=resp.status_code, content={"detail": resp.text})


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


# ── NAS Images proxy ───────────────────────────────────────────────────────────

@router.get("/nas/images")
async def nas_images(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """NAS 照片列表 proxy
    照片按日期存在子資料夾（/homes/firmness/{serial}/YYYY-MM-DD/）
    Auth Service 負責列出所有日期資料夾並合併查詢
    """
    from datetime import datetime, timedelta
    import re as _re

    cam_token = await get_camera_backend_token(current_user)
    if not cam_token:
        raise HTTPException(502, "無法取得 Camera Backend token")

    params = dict(request.query_params)
    camera_id = params.get("camera_id")
    limit = int(params.get("limit", 30))
    offset = int(params.get("offset", 0))
    start_time = params.get("start_time")
    end_time = params.get("end_time")

    async with httpx.AsyncClient(timeout=30) as client:
        # 1. 取得 device_serial_id
        serial = None
        if camera_id:
            cam_resp = await client.get(
                f"{CAMERA_BACKEND_URL}/api/cameras/{camera_id}",
                headers={"Authorization": f"Bearer {cam_token}"},
            )
            if cam_resp.status_code == 200:
                cam_data = cam_resp.json()
                basic = cam_data.get("basic_info", cam_data)
                serial = (
                    basic.get("device_serial_id") or
                    basic.get("serial_id") or
                    basic.get("serial")
                )

        if not serial:
            # 沒有 serial 就用原本邏輯
            resp = await client.get(
                f"{CAMERA_BACKEND_URL}/api/camera/nas/images",
                headers={"Authorization": f"Bearer {cam_token}"},
                params=params,
            )
            try:
                return JSONResponse(status_code=resp.status_code, content=resp.json())
            except Exception:
                return JSONResponse(status_code=resp.status_code, content={"detail": resp.text})

        base_path = f"/homes/firmness/{serial}"

        # 2. 列出根目錄取得所有日期資料夾
        list_resp = await client.get(
            f"{CAMERA_BACKEND_URL}/api/camera/nas/images",
            headers={"Authorization": f"Bearer {cam_token}"},
            params={"camera_id": camera_id, "folder_path": base_path, "limit": 1000, "offset": 0},
        )
        list_data = list_resp.json() if list_resp.status_code == 200 else {}

        # 從回傳的 files 裡找出日期資料夾（格式 YYYY-MM-DD）
        all_files = list_data.get("data", {}).get("files", [])
        date_folders = sorted([
            f["name"] for f in all_files
            if _re.match(r'^\d{4}-\d{2}-\d{2}$', f.get("name", ""))
        ], reverse=True)  # 最新日期在前

        # 如果有日期時間篩選，過濾資料夾
        if start_time:
            start_date = start_time[:10]  # YYYY-MM-DD
            date_folders = [d for d in date_folders if d >= start_date]
        if end_time:
            end_date = end_time[:10]
            date_folders = [d for d in date_folders if d <= end_date]

        if not date_folders:
            # 沒有日期資料夾，直接讀根目錄
            resp = await client.get(
                f"{CAMERA_BACKEND_URL}/api/camera/nas/images",
                headers={"Authorization": f"Bearer {cam_token}"},
                params={**params, "folder_path": base_path},
            )
            try:
                return JSONResponse(status_code=resp.status_code, content=resp.json())
            except Exception:
                return JSONResponse(status_code=resp.status_code, content={"detail": resp.text})

        # 3. 計算 total（所有日期資料夾的照片總數）
        # 先用第一個資料夾拿 total 估算，之後再精確
        total_count = 0
        folder_totals = {}
        for date_folder in date_folders:
            folder_resp = await client.get(
                f"{CAMERA_BACKEND_URL}/api/camera/nas/images",
                headers={"Authorization": f"Bearer {cam_token}"},
                params={"camera_id": camera_id, "folder_path": f"{base_path}/{date_folder}", "limit": 1, "offset": 0},
            )
            if folder_resp.status_code == 200:
                folder_total = folder_resp.json().get("data", {}).get("total", 0)
                folder_totals[date_folder] = folder_total
                total_count += folder_total

        # 4. 根據 offset/limit 決定從哪個資料夾開始取
        collected = []
        skipped = 0
        for date_folder in date_folders:
            folder_total = folder_totals.get(date_folder, 0)
            if skipped + folder_total <= offset:
                skipped += folder_total
                continue
            folder_offset = offset - skipped if skipped < offset else 0
            need = limit - len(collected)
            folder_resp = await client.get(
                f"{CAMERA_BACKEND_URL}/api/camera/nas/images",
                headers={"Authorization": f"Bearer {cam_token}"},
                params={
                    "camera_id": camera_id,
                    "folder_path": f"{base_path}/{date_folder}",
                    "limit": need,
                    "offset": folder_offset,
                    **({"start_time": start_time} if start_time else {}),
                    **({"end_time": end_time} if end_time else {}),
                },
            )
            if folder_resp.status_code == 200:
                files = folder_resp.json().get("data", {}).get("files", [])
                # 在每張照片的 path 加上日期資料夾，讓前端能正確載入
                for f in files:
                    if not f.get("path", "").startswith(base_path):
                        f["path"] = f"{base_path}/{date_folder}/{f['name']}"
                    if f.get("image_url") and not f["image_url"].startswith("http"):
                        f["image_url"] = f["image_url"].replace(
                            base_path, f"{base_path}/{date_folder}"
                        ) if base_path in f["image_url"] else f["image_url"]
                    f["date"] = date_folder  # 加上日期欄位給前端用
                collected.extend(files)
            skipped += folder_total
            if len(collected) >= limit:
                break

        return JSONResponse(status_code=200, content={
            "success": True,
            "data": {
                "files": collected[:limit],
                "total": total_count,
                "returned": len(collected[:limit]),
                "offset": offset,
                "limit": limit,
            },
            "debug": {
                "folder_path": base_path,
                "date_folders": date_folders,
            }
        })


@router.get("/nas/image")
async def nas_image(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """NAS 單張照片 proxy"""
    cam_token = await get_camera_backend_token(current_user)
    if not cam_token:
        raise HTTPException(502, "無法取得 Camera Backend token")
    from fastapi.responses import StreamingResponse
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{CAMERA_BACKEND_URL}/api/camera/nas/image",
            headers={"Authorization": f"Bearer {cam_token}"},
            params=dict(request.query_params),
        )
        return StreamingResponse(
            content=iter([resp.content]),
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "image/jpeg"),
        )


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

# ── Projects proxy ─────────────────────────────────────────────────────────────

@router.get("/projects")
async def list_projects(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cam_token = await get_camera_backend_token(current_user)
    if not cam_token:
        return {"projects": []}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{CAMERA_BACKEND_URL}/api/projects",
            headers={"Authorization": f"Bearer {cam_token}"},
        )
        if resp.status_code == 200:
            return resp.json()
    return {"projects": []}


@router.post("/projects")
async def create_project(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.role not in ("reseller", "symotus_admin"):
        raise HTTPException(403, "沒有建立專案的權限")
    cam_token = await get_camera_backend_token(current_user)
    if not cam_token:
        raise HTTPException(502, "無法取得 Camera Backend token")
    body = await request.body()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{CAMERA_BACKEND_URL}/api/projects",
            headers={"Authorization": f"Bearer {cam_token}", "Content-Type": "application/json"},
            content=body,
        )
        try:
            return JSONResponse(status_code=resp.status_code, content=resp.json())
        except Exception:
            return JSONResponse(status_code=resp.status_code, content={"detail": resp.text})

