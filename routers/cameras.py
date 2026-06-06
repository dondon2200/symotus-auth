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
import os
CAMERA_SERVICE_KEY = os.environ.get("CAMERA_SERVICE_KEY", "")


async def get_camera_backend_token(user: User) -> str:
    """取得 Camera Backend token
    安全原則：
    - 必須有 camera_email 才能換 token（代表該帳號有在 Camera Backend 配對過相機）
    - LINE 自動合成的 camera_email（line_xxx@symotus.com）Camera Backend 會自動建立帳號，正常換 token
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
        # 標記為自己擁有的相機
        for c in cameras:
            c["is_shared"] = False
        # reseller 看自己的相機；如果有 allowed_ids 限制再過濾
        if allowed_ids is not None:
            cameras = [c for c in cameras if c["id"] in allowed_ids]
        # ⚠️ 安全過濾：LINE 合成 email 的帳號在 Camera Backend 可能混用
        # 必須以 Symotus camera_access 表為唯一授權依據，
        # 防止 Camera Backend 帳號共用導致看到他人相機
        is_line_email = (
            current_user.camera_email and
            current_user.camera_email.startswith("line_") and
            current_user.camera_email.endswith("@symotus.com")
        )
        if is_line_email:
            auth_cam_ids = set(
                a.camera_id for a in db.query(CameraAccess).filter(
                    CameraAccess.user_id == current_user.id
                ).all()
            )
            cameras = [c for c in cameras if c.get("id") in auth_cam_ids]
    else:
        # 沒有 camera token（end_user、reseller 沒有 camera_email）：
        # 走 camera_access 路徑。allowed_ids=None(reseller) 表示無自有相機，但仍可有分享相機
        cameras = []
        # 處理 end_user 的 allowed_ids 清單（從 camera_access 表得來）
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
                    raw = r.json()
                    cam_data = raw.get("basic_info", raw)  # 攤平 detail 格式
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
                raw = r.json()
                cam_data = raw.get("basic_info", raw)  # 攤平 detail 格式
                perm = access.permission_level if hasattr(access, "permission_level") and access.permission_level else "photos_stream"
                cam_data["permission_level"] = perm
                # 自己配對的相機（granted_by == self）顯示為「我的相機」，不是「分享給我」
                cam_data["is_shared"] = (access.granted_by != current_user.id)
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
    # 若 user token 被拒，改用 admin token（相機可能屬於不同 CB 帳號）
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{CAMERA_BACKEND_URL}/api/cameras/thumbnails/latest",
            headers={"Authorization": f"Bearer {cam_token}"},
            params={"ids": ",".join(str(i) for i in requested_ids)},
        )
    if resp.status_code in (403, 404) or not resp.content:
        async with httpx.AsyncClient(timeout=10) as client:
            tok_r = await client.post(
                f"{CAMERA_BACKEND_URL}/internal/auth/token",
                headers={"x-service-key": CAMERA_SERVICE_KEY},
                json={"user_id": 0, "email": "admin@timelapse.com", "role": "symotus_admin"},
            )
        admin_tok = tok_r.json().get("access_token", "") if tok_r.status_code == 200 else ""
        if admin_tok:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{CAMERA_BACKEND_URL}/api/cameras/thumbnails/latest",
                    headers={"Authorization": f"Bearer {admin_tok}"},
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
    # 若沒有自己的 token（reseller 尚未設 camera_email），用 admin fallback
    if not cam_token:
        async with httpx.AsyncClient(timeout=10) as client:
            tok_r = await client.post(
                f"{CAMERA_BACKEND_URL}/internal/auth/token",
                headers={"x-service-key": CAMERA_SERVICE_KEY},
                json={"user_id": 0, "email": "admin@timelapse.com", "role": "symotus_admin"},
            )
        cam_token = tok_r.json().get("access_token", "") if tok_r.status_code == 200 else ""
    if not cam_token:
        raise HTTPException(502, "無法取得 Camera Backend token，請確認 camera_email 設定")
    used_admin_fallback = not current_user.camera_email
    body = await request.body()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{CAMERA_BACKEND_URL}/api/cameras",
            headers={"Authorization": f"Bearer {cam_token}", "Content-Type": "application/json"},
            content=body,
        )
        try:
            resp_data = resp.json()
        except Exception:
            return JSONResponse(status_code=resp.status_code, content={"detail": resp.text})

    # 若用 admin fallback 配對，自動幫 reseller 建立 camera_access（full 權限）
    if resp.status_code in (200, 201) and used_admin_fallback and current_user.role == "reseller":
        camera_id = resp_data.get("id") or resp_data.get("basic_info", {}).get("id")
        if camera_id:
            existing = db.query(CameraAccess).filter(
                CameraAccess.camera_id == camera_id,
                CameraAccess.user_id == current_user.id,
            ).first()
            if not existing:
                db.add(CameraAccess(
                    camera_id=camera_id,
                    user_id=current_user.id,
                    granted_by=current_user.id,
                    permission_level="full",
                ))
                db.commit()

    return JSONResponse(status_code=resp.status_code, content=resp_data)


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

    # 若沒有 cam_token，試 camera_access granter 的 token（分享相機的擁有者）
    access = db.query(CameraAccess).filter(
        CameraAccess.camera_id == camera_id,
        CameraAccess.user_id == current_user.id,
    ).first()
    if not cam_token and access and access.granted_by:
        owner = db.query(User).filter(User.id == access.granted_by).first()
        if owner:
            cam_token = await get_camera_backend_token(owner)
    # 最後 fallback admin
    if not cam_token:
        async with httpx.AsyncClient(timeout=10) as client:
            tok_r = await client.post(
                f"{CAMERA_BACKEND_URL}/internal/auth/token",
                headers={"x-service-key": CAMERA_SERVICE_KEY},
                json={"user_id": 0, "email": "admin@timelapse.com", "role": "symotus_admin"},
            )
        cam_token = tok_r.json().get("access_token", "") if tok_r.status_code == 200 else ""
    if not cam_token:
        raise HTTPException(502, "無法取得 Camera Backend token")
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{CAMERA_BACKEND_URL}/api/cameras/{camera_id}",
            headers={"Authorization": f"Bearer {cam_token}"},
        )
    # 若 user token 存取失敗（相機可能屬於不同 CB 帳號），嘗試 admin fallback
    if resp.status_code in (403, 404):
        async with httpx.AsyncClient(timeout=10) as client:
            tok_r = await client.post(
                f"{CAMERA_BACKEND_URL}/internal/auth/token",
                headers={"x-service-key": CAMERA_SERVICE_KEY},
                json={"user_id": 0, "email": "admin@timelapse.com", "role": "symotus_admin"},
            )
        admin_token = tok_r.json().get("access_token", "") if tok_r.status_code == 200 else ""
        if admin_token:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{CAMERA_BACKEND_URL}/api/cameras/{camera_id}",
                    headers={"Authorization": f"Bearer {admin_token}"},
                )
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, resp.text)
    data = resp.json()

    # 附上當前用戶的權限等級
    # 先查 camera_access（分享邀請授權）；若有，以授權等級為準
    # access 已在上方查過
    if access:
        data["my_permission"] = access.permission_level or "photos_stream"
    elif current_user.role in ("reseller", "symotus_admin"):
        data["my_permission"] = "full"  # 自己擁有的相機
    else:
        data["my_permission"] = "stream_only"

    return data


@router.post("/{camera_id}/notify-subscribe")
async def subscribe_online_notification(
    camera_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """訂閱或查詢相機開機 LINE 通知狀態"""
    allowed_ids = get_allowed_camera_ids(current_user, db)
    if allowed_ids is not None and camera_id not in allowed_ids:
        raise HTTPException(403, "無此相機的存取權限")

    if not current_user.line_id:
        return {"subscribed": False, "needs_line": True, "is_following": False,
                "message": "請先加入官方 LINE 帳號"}

    # 檢查是否有追蹤 LINE Bot（呼叫 LINE API 取得 profile，404 = 未追蹤）
    LINE_ACCESS_TOKEN = os.environ.get("LINE_ACCESS_TOKEN", "")
    is_following = False
    if LINE_ACCESS_TOKEN:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(
                    f"https://api.line.me/v2/bot/profile/{current_user.line_id}",
                    headers={"Authorization": f"Bearer {LINE_ACCESS_TOKEN}"}
                )
                is_following = (r.status_code == 200)
        except Exception:
            is_following = False

    if not is_following:
        return {"subscribed": False, "needs_line": True, "is_following": False,
                "message": "請先加入官方 LINE 帳號以接收通知"}

    # 設定 notify_on_online=True
    access = db.query(CameraAccess).filter(
        CameraAccess.camera_id == camera_id,
        CameraAccess.user_id == current_user.id,
    ).first()
    if not access:
        db.add(CameraAccess(
            camera_id=camera_id, user_id=current_user.id,
            granted_by=current_user.id, permission_level="stream_only",
            notify_on_online=True,
        ))
    else:
        access.notify_on_online = True
    db.commit()

    return {"subscribed": True, "is_following": True, "message": "開機時將透過 LINE 通知您"}


@router.post("/{camera_id}/notify-unsubscribe")
async def unsubscribe_online_notification(
    camera_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """取消相機開機 LINE 通知"""
    access = db.query(CameraAccess).filter(
        CameraAccess.camera_id == camera_id,
        CameraAccess.user_id == current_user.id,
    ).first()
    if access:
        access.notify_on_online = False
        db.commit()
    return {"subscribed": False, "message": "已取消開機通知"}


@router.get("/{camera_id}/notify-status")
async def get_notify_status(
    camera_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """查詢此相機的通知訂閱狀態"""
    if not current_user.line_id:
        return {"subscribed": False, "needs_line": True}
    access = db.query(CameraAccess).filter(
        CameraAccess.camera_id == camera_id,
        CameraAccess.user_id == current_user.id,
    ).first()
    return {"subscribed": bool(access and access.notify_on_online)}


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

    params = dict(request.query_params)
    camera_id = params.get("camera_id")

    # 沒有自己的 token（分享用戶）→ 用 granter 的 token
    if not cam_token and camera_id:
        access = db.query(CameraAccess).filter(
            CameraAccess.user_id == current_user.id,
            CameraAccess.camera_id == int(camera_id),
        ).first()
        if access and access.granted_by:
            owner = db.query(User).filter(User.id == access.granted_by).first()
            if owner:
                cam_token = await get_camera_backend_token(owner)

    if not cam_token:
        raise HTTPException(502, "無法取得 Camera Backend token")
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
    # 分享用戶沒有自己的 token → 嘗試用 granter token
    if not cam_token:
        path_param = request.query_params.get("path", "")
        # 路徑格式：/homes/firmness/{serial}/... 無法直接得知 camera_id
        # 改為查該用戶所有 camera_access，取第一個 granter token
        access = db.query(CameraAccess).filter(CameraAccess.user_id == current_user.id).first()
        if access and access.granted_by:
            owner = db.query(User).filter(User.id == access.granted_by).first()
            if owner:
                cam_token = await get_camera_backend_token(owner)
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
    # 若沒有自己的 token，嘗試用 camera_access granter 的 token
    if not cam_token:
        access = db.query(CameraAccess).filter(
            CameraAccess.user_id == current_user.id,
            CameraAccess.camera_id == camera_id,
        ).first()
        if access and access.granted_by:
            owner = db.query(User).filter(User.id == access.granted_by).first()
            if owner:
                cam_token = await get_camera_backend_token(owner)
    # 最後 fallback 到 admin token
    if not cam_token:
        async with httpx.AsyncClient(timeout=10) as client:
            tok_r = await client.post(
                f"{CAMERA_BACKEND_URL}/internal/auth/token",
                headers={"x-service-key": CAMERA_SERVICE_KEY},
                json={"user_id": 0, "email": "admin@timelapse.com", "role": "symotus_admin"},
            )
        cam_token = tok_r.json().get("access_token", "") if tok_r.status_code == 200 else ""
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
    # 若 user token 被拒（相機屬於不同 CB 帳號），自動換 admin token 重試
    if resp.status_code in (403, 404):
        async with httpx.AsyncClient(timeout=10) as client:
            tok_r = await client.post(
                f"{CAMERA_BACKEND_URL}/internal/auth/token",
                headers={"x-service-key": CAMERA_SERVICE_KEY},
                json={"user_id": 0, "email": "admin@timelapse.com", "role": "symotus_admin"},
            )
        admin_tok = tok_r.json().get("access_token", "") if tok_r.status_code == 200 else ""
        if admin_tok:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.request(
                    method=request.method,
                    url=f"{CAMERA_BACKEND_URL}/api/cameras/{camera_id}/{path}",
                    headers={"Authorization": f"Bearer {admin_tok}", "Content-Type": "application/json"},
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
    # 若沒有自己的 token（reseller 尚未設 camera_email），用 admin fallback
    if not cam_token:
        async with httpx.AsyncClient(timeout=10) as client:
            tok_r = await client.post(
                f"{CAMERA_BACKEND_URL}/internal/auth/token",
                headers={"x-service-key": CAMERA_SERVICE_KEY},
                json={"user_id": 0, "email": "admin@timelapse.com", "role": "symotus_admin"},
            )
        cam_token = tok_r.json().get("access_token", "") if tok_r.status_code == 200 else ""
    if not cam_token:
        raise HTTPException(502, "無法取得 Camera Backend token，請確認 camera_email 設定")
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

