"""
Cameras proxy router - Auth Service 管理相機存取權限
所有相機 API 都經過這裡，Auth Service 負責權限控制
Camera Backend 不管權限，只負責相機操作
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from typing import Optional
import asyncio
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
    - LINE 自動合成的 camera_email（line_xxx@symotus.com）不算真實帳號，不給 token
    - 沒有 camera_email 的用戶無法直接存取 Camera Backend，只能透過 camera_access 看授權相機
    """
    if not user.camera_email:
        return ""  # 沒有 camera_email = 沒有 Camera Backend 帳號，不給 token
    if user.camera_email.startswith("line_") and user.camera_email.endswith("@symotus.com"):
        return ""  # LINE 自動合成 email，尚未真實綁定 Camera Backend 帳號
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
        # 標記為自己擁有的相機
        for c in cameras:
            c["is_shared"] = False
        # reseller 看自己的相機；如果有 allowed_ids 限制再過濾
        if allowed_ids is not None:
            cameras = [c for c in cameras if c["id"] in allowed_ids]
    else:
        # 沒有 camera token（end_user 或未配對用戶，或 reseller 沒有 camera_email）：
        # 用 camera_access 授權的相機，需要用 owner 的 token 去拿資料
        # allowed_ids=None (reseller) → 不 early return，走到下面 camera_access 合併
        if allowed_ids is not None and len(allowed_ids) == 0:
            cameras = []
        elif allowed_ids is not None:
            cameras = []
        # 找各台相機的 owner，用 owner token 拿資料
        for cam_id in (allowed_ids or []):
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
                    cam_data = r.json()
                    cam_data["permission_level"] = a.permission_level if hasattr(a, 'permission_level') else "photos_stream"
                    cam_data["is_shared"] = True
                    cameras.append(cam_data)

    # 額外：把 camera_access 裡的授權相機也加進來（reseller 接受邀請後）
    shared_ids = set(c.get("id") for c in cameras)
    shared_accesses = db.query(CameraAccess).filter(
        CameraAccess.user_id == current_user.id
    ).all()
    # 取得 admin 備用 token（inviter 沒有 camera_email 時 fallback 用）
    admin_fallback_token = None
    for access in shared_accesses:
        if access.camera_id in shared_ids:
            continue  # 已經有了
        owner = db.query(User).filter(User.id == access.granted_by).first()
        owner_token = (await get_camera_backend_token(owner)) if owner else ""
        if not owner_token:
            # 嘗試用 admin 帳號 token 取相機資料
            if admin_fallback_token is None:
                try:
                    async with httpx.AsyncClient(timeout=10) as client:
                        tok_r = await client.post(
                            f"{CAMERA_BACKEND_URL}/internal/auth/token",
                            headers={"x-service-key": CAMERA_SERVICE_KEY},
                            json={"user_id": 0, "email": "admin@timelapse.com", "role": "symotus_admin"},
                        )
                        admin_fallback_token = tok_r.json().get("access_token", "") if tok_r.status_code == 200 else ""
                except Exception:
                    admin_fallback_token = ""
            owner_token = admin_fallback_token
        if not owner_token:
            continue
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{CAMERA_BACKEND_URL}/api/cameras/{access.camera_id}",
                                 headers={"Authorization": f"Bearer {owner_token}"})
            if r.status_code == 200:
                cam_data = r.json()
                perm = access.permission_level if hasattr(access, "permission_level") and access.permission_level else "photos_stream"
                cam_data["permission_level"] = perm
                cam_data["is_shared"] = True
                cameras.append(cam_data)
                shared_ids.add(access.camera_id)

    return {"cameras": cameras, "total": len(cameras)}


@router.get("/timer-status")
async def get_timer_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """取得所有相機定時開關機倒數狀態"""
    cam_token = await get_camera_backend_token(current_user)
    if not cam_token:
        return {"timers": []}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{CAMERA_BACKEND_URL}/api/cameras/timer-status",
            headers={"Authorization": f"Bearer {cam_token}"},
        )
    if resp.status_code == 200:
        return resp.json()
    return {"timers": []}


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
        if resp.status_code != 200:
            raise HTTPException(resp.status_code, resp.text)
        data = resp.json()

    # 附上當前用戶的權限等級
    if current_user.role in ("reseller", "symotus_admin"):
        data["my_permission"] = "full"
    else:
        from models import CameraAccess
        access = db.query(CameraAccess).filter(
            CameraAccess.camera_id == camera_id,
            CameraAccess.user_id == current_user.id,
        ).first()
        data["my_permission"] = access.permission_level if access else "stream_only"

    return data


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
    照片按日期存在子資料夾 /homes/firmness/{serial}/YYYY-MM-DD/
    用 asyncio.gather 並行查詢所有日期資料夾，速度快
    """
    from datetime import datetime, timedelta, date as date_type

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

        # 2. 產生日期列表（最新在前）
        now = datetime.utcnow()
        if end_time:
            try:
                end_dt = datetime.fromisoformat(end_time.replace("T", " ").split(".")[0]).date()
            except Exception:
                end_dt = now.date()
        else:
            end_dt = now.date()

        if start_time:
            try:
                start_dt = datetime.fromisoformat(start_time.replace("T", " ").split(".")[0]).date()
            except Exception:
                start_dt = end_dt - timedelta(days=30)
        else:
            start_dt = end_dt - timedelta(days=365)  # 預設查一年

        date_list = []
        cur = end_dt
        while cur >= start_dt and len(date_list) < 400:
            date_list.append(cur.strftime("%Y-%m-%d"))
            cur -= timedelta(days=1)

        # 3. 並行查所有日期資料夾的 total
        async def get_folder_total(date_str: str):
            try:
                r = await client.get(
                    f"{CAMERA_BACKEND_URL}/api/camera/nas/images",
                    headers={"Authorization": f"Bearer {cam_token}"},
                    params={
                        "camera_id": camera_id,
                        "folder_path": f"{base_path}/{date_str}",
                        "limit": 1,
                        "offset": 0,
                    },
                )
                if r.status_code == 200:
                    total = r.json().get("data", {}).get("total", 0)
                    return (date_str, total)
            except Exception:
                pass
            return (date_str, 0)

        sem = asyncio.Semaphore(10)  # 最多同時 10 個請求，避免 OOM
        async def get_folder_total_safe(date_str: str):
            async with sem:
                return await get_folder_total(date_str)
        results = await asyncio.gather(*[get_folder_total_safe(d) for d in date_list])
        folder_totals = {d: t for d, t in results if t > 0}
        active_dates = [d for d in date_list if folder_totals.get(d, 0) > 0]
        total_count = sum(folder_totals.values())

        # 4. 根據 offset/limit 取照片
        # Camera Backend 每次最多回傳 30 筆，超過會回 0，需分批取
        CAM_MAX = 30
        collected = []
        skipped = 0
        for date_str in active_dates:
            folder_total = folder_totals[date_str]
            if skipped + folder_total <= offset:
                skipped += folder_total
                continue
            folder_offset = offset - skipped if skipped < offset else 0
            need = limit - len(collected)
            # 分批取，每批最多 CAM_MAX 筆
            while need > 0:
                chunk = min(need, CAM_MAX)
                r = await client.get(
                    f"{CAMERA_BACKEND_URL}/api/camera/nas/images",
                    headers={"Authorization": f"Bearer {cam_token}"},
                    params={
                        "camera_id": camera_id,
                        "folder_path": f"{base_path}/{date_str}",
                        "limit": chunk,
                        "offset": folder_offset,
                    },
                )
                if r.status_code != 200:
                    break
                files = r.json().get("data", {}).get("files", [])
                if not files:
                    break
                for f in files:
                    f["date"] = date_str
                collected.extend(files)
                folder_offset += len(files)
                need -= len(files)
                if len(files) < chunk:
                    break  # 該日資料夾已取完
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
                "date_folders_found": active_dates,
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

