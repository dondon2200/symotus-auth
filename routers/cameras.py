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
import logging
import httpx

from database import get_db
from models import User, CameraAccess
from auth import get_current_user

router = APIRouter(prefix="/cameras", tags=["cameras"])

CAMERA_BACKEND_URL = "https://user.symotus.com"
import os
CAMERA_SERVICE_KEY = os.environ.get("CAMERA_SERVICE_KEY", "")

logger = logging.getLogger(__name__)


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


async def _get_admin_camera_token() -> str:
    """取得 Camera Backend 的 admin 備用 token（granted_by 非真正擁有者時 fallback 用）"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{CAMERA_BACKEND_URL}/internal/auth/token",
                headers={"x-service-key": CAMERA_SERVICE_KEY},
                json={"user_id": 0, "email": "admin@timelapse.com", "role": "symotus_admin"},
            )
            return r.json().get("access_token", "") if r.status_code == 200 else ""
    except Exception as e:
        logger.warning("admin fallback token 取得失敗: %s", e)
        return ""


async def _try_granter_token(camera_id: int, access, current_user: "User", db: Session) -> str:
    """F-13：相機可能由非 admin 的 granter 帳號擁有。
    若該 user 對此相機有 grant（且 granter≠自己），回傳「能存取該相機」的 granter token，否則回 ""。
    優先於 admin fallback 使用，讓被分享、由真實 reseller 帳號擁有的相機可正常存取。"""
    if not (access and access.granted_by and access.granted_by != current_user.id):
        return ""
    granter = db.query(User).filter(User.id == access.granted_by).first()
    if not granter:
        return ""
    gtok = await get_camera_backend_token(granter)
    if not gtok:
        return ""
    async with httpx.AsyncClient(timeout=8) as client:
        r = await client.get(
            f"{CAMERA_BACKEND_URL}/api/cameras/{camera_id}",
            headers={"Authorization": f"Bearer {gtok}"},
        )
    return gtok if r.status_code == 200 else ""


async def fetch_camera_detail(camera_id: int, owner: Optional[User], admin_holder: dict) -> Optional[dict]:
    """抓相機細節並攤平成 basic_info。

    先用 owner 的 Camera Backend token；若 owner 無 token 或回非 200，
    再退回 admin token 重試。失敗回 None。

    重點：`granted_by`（owner）不一定是 Camera Backend 的真正擁有者——
    跨層轉分享時（admin → reseller A → reseller B），A 自己也只是被分享，
    用 A 的 token 取該相機會 403。授權已由 camera_access 在 DB 層把關，
    admin token 僅用於取得顯示資料，不放寬權限。
    """
    owner_token = (await get_camera_backend_token(owner)) if owner else ""
    for label in ("owner", "admin"):
        if label == "owner":
            token = owner_token
        else:
            if admin_holder.get("t") is None:
                admin_holder["t"] = await _get_admin_camera_token()
            token = admin_holder["t"]
        if not token:
            continue
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    f"{CAMERA_BACKEND_URL}/api/cameras/{camera_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
            if r.status_code == 200:
                raw = r.json()
                return raw.get("basic_info", raw)  # 攤平 detail 格式
            logger.warning("fetch camera %s via %s token -> HTTP %s", camera_id, label, r.status_code)
        except Exception as e:
            logger.warning("fetch camera %s via %s token error: %s", camera_id, label, e)
    return None


@router.get("")
async def list_cameras(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """取得用戶可存取的相機列表（Auth Service 控制權限）"""
    allowed_ids = get_allowed_camera_ids(current_user, db)
    cam_token = await get_camera_backend_token(current_user)
    admin_token_holder = {"t": None}  # admin fallback token，lazy 取一次共用

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
            cam_data = await fetch_camera_detail(cam_id, owner, admin_token_holder)
            if not cam_data:
                logger.warning("list_cameras: 無法取得分享相機 %s（user=%s granted_by=%s）",
                               cam_id, current_user.id, access.granted_by)
                continue
            cam_data["permission_level"] = access.permission_level or "photos_stream"
            cam_data["is_shared"] = True
            cameras.append(cam_data)

    # 額外：把 camera_access 裡的授權相機也加進來（reseller 接受邀請後）
    shared_ids = set(c.get("id") for c in cameras)
    shared_accesses = db.query(CameraAccess).filter(
        CameraAccess.user_id == current_user.id
    ).all()
    for access in shared_accesses:
        if access.camera_id in shared_ids:
            continue  # 已經有了
        owner = db.query(User).filter(User.id == access.granted_by).first()
        # granted_by 不一定是 Camera Backend 真正擁有者（跨層轉分享），
        # fetch_camera_detail 會在 owner token 失敗(空或非 200)時退回 admin token 重試。
        cam_data = await fetch_camera_detail(access.camera_id, owner, admin_token_holder)
        if not cam_data:
            logger.warning("list_cameras: 無法取得分享相機 %s（user=%s granted_by=%s）",
                           access.camera_id, current_user.id, access.granted_by)
            continue
        cam_data["permission_level"] = access.permission_level or "photos_stream"
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
        # F-3：非 admin 僅對「有 camera_access grant 的相機」允許 admin fallback
        if current_user.role == "symotus_admin":
            fb_ids = requested_ids
        else:
            granted = {a.camera_id for a in db.query(CameraAccess).filter(
                CameraAccess.user_id == current_user.id).all()}
            fb_ids = [i for i in requested_ids if i in granted]
        if fb_ids:
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
                        params={"ids": ",".join(str(i) for i in fb_ids)},
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
    # F-3：僅當有 camera_access grant 或為 admin 時，才允許 granter/admin fallback
    allow_fallback = (access is not None) or current_user.role == "symotus_admin"
    if not cam_token and access and access.granted_by:
        owner = db.query(User).filter(User.id == access.granted_by).first()
        if owner:
            cam_token = await get_camera_backend_token(owner)
    # 最後 fallback admin（僅 grant/admin）
    if not cam_token and allow_fallback:
        async with httpx.AsyncClient(timeout=10) as client:
            tok_r = await client.post(
                f"{CAMERA_BACKEND_URL}/internal/auth/token",
                headers={"x-service-key": CAMERA_SERVICE_KEY},
                json={"user_id": 0, "email": "admin@timelapse.com", "role": "symotus_admin"},
            )
        cam_token = tok_r.json().get("access_token", "") if tok_r.status_code == 200 else ""
    if not cam_token:
        raise HTTPException(403, "無此相機的存取權限")
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{CAMERA_BACKEND_URL}/api/cameras/{camera_id}",
            headers={"Authorization": f"Bearer {cam_token}"},
        )
    # user token 存取失敗（相機可能屬於不同 CB 帳號）：F-13 先試 granter token，再退 admin fallback（僅 grant/admin）
    if resp.status_code in (403, 404) and allow_fallback:
        gtok = await _try_granter_token(camera_id, access, current_user, db)
        if gtok:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{CAMERA_BACKEND_URL}/api/cameras/{camera_id}",
                    headers={"Authorization": f"Bearer {gtok}"},
                )
    if resp.status_code in (403, 404) and allow_fallback:
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
        # F-12：僅「完整(full)權限」帳號可解除綁定
        access = db.query(CameraAccess).filter(
            CameraAccess.camera_id == camera_id,
            CameraAccess.user_id == current_user.id,
        ).first()
        if not access:
            raise HTTPException(404, "存取權限不存在")
        if access.permission_level != "full":
            raise HTTPException(403, "需要完整(full)權限才能解除綁定")
        db.delete(access)
        db.commit()
        return {"success": True, "message": "已移除相機存取權限"}

    # reseller / symotus_admin
    access = db.query(CameraAccess).filter(
        CameraAccess.camera_id == camera_id,
        CameraAccess.user_id == current_user.id,
    ).first()

    # (A) 被分享下來的相機（granted_by != self）：自己不是 Camera Backend 真正擁有者，
    #     解除綁定只撤銷自己的存取，不動 Camera Backend（避免動到真正擁有者的相機）
    if access and access.granted_by and access.granted_by != current_user.id:
        db.delete(access)
        db.commit()
        return {"success": True, "message": "已移除相機存取權限"}

    # (B) 自己配對的相機（granted_by == self 或無 grant）：真正呼叫 Camera Backend unbind。
    #     token 依序：自己的 → admin fallback（涵蓋 admin-fallback 配對、無 camera_email 的情況）。
    #     admin fallback 僅作用於「自己的相機」，不放寬到他人相機。
    cam_token = await get_camera_backend_token(current_user)
    if not cam_token:
        cam_token = await _get_admin_camera_token()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{CAMERA_BACKEND_URL}/api/cameras/{camera_id}/unbind",
            headers={"Authorization": f"Bearer {cam_token}", "Content-Type": "application/json"},
            json={},
        )
        # 自己 token 被 Camera Backend 拒（非真正擁有者）→ 用 admin fallback 再試一次
        if resp.status_code == 403:
            admin_tok = await _get_admin_camera_token()
            if admin_tok and admin_tok != cam_token:
                resp = await client.post(
                    f"{CAMERA_BACKEND_URL}/api/cameras/{camera_id}/unbind",
                    headers={"Authorization": f"Bearer {admin_tok}", "Content-Type": "application/json"},
                    json={},
                )
        if resp.status_code == 200:
            if access:  # 同步清掉自己這筆 camera_access，避免殘留死記錄
                db.delete(access)
                db.commit()
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

    # F-3：此相機的 grant（決定是否允許 granter/admin fallback）
    access = None
    if camera_id:
        access = db.query(CameraAccess).filter(
            CameraAccess.user_id == current_user.id,
            CameraAccess.camera_id == int(camera_id),
        ).first()
    allow_fallback = (access is not None) or current_user.role == "symotus_admin"

    # 沒有自己的 token（分享用戶）→ 用 granter 的 token
    if not cam_token and access and access.granted_by:
        owner = db.query(User).filter(User.id == access.granted_by).first()
        if owner:
            cam_token = await get_camera_backend_token(owner)

    if not cam_token:
        raise HTTPException(502, "無法取得 Camera Backend token")

    # 預先驗證 token 是否能存取此相機，不能就換 admin token（僅 grant/admin 允許）
    if cam_token and camera_id:
        async with httpx.AsyncClient(timeout=8) as client:
            test_r = await client.get(
                f"{CAMERA_BACKEND_URL}/api/cameras/{camera_id}",
                headers={"Authorization": f"Bearer {cam_token}"},
            )
        if test_r.status_code in (403, 404):
            if not allow_fallback:
                raise HTTPException(403, "無此相機的存取權限")
            # F-13：先試 granter token（相機可能屬非 admin 的 granter 帳號），再退 admin token
            gtok = await _try_granter_token(int(camera_id), access, current_user, db)
            if gtok:
                cam_token = gtok
            else:
                async with httpx.AsyncClient(timeout=10) as client:
                    tok_r = await client.post(
                        f"{CAMERA_BACKEND_URL}/internal/auth/token",
                        headers={"x-service-key": CAMERA_SERVICE_KEY},
                        json={"user_id": 0, "email": "admin@timelapse.com", "role": "symotus_admin"},
                    )
                cam_token = tok_r.json().get("access_token", "") if tok_r.status_code == 200 else cam_token

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

    # 此相機的 camera_access grant（供 F-5 等級檢查 與 F-3 fallback 閘 共用）
    access = db.query(CameraAccess).filter(
        CameraAccess.camera_id == camera_id,
        CameraAccess.user_id == current_user.id,
    ).first()
    allow_fallback = (access is not None) or current_user.role == "symotus_admin"

    # F-5：寫入類操作（改設定/排程/PTZ/重啟等）需「完整(full)權限」。
    if request.method not in ("GET", "HEAD") and access and access.permission_level != "full":
        raise HTTPException(403, "此操作需要完整(full)權限")

    cam_token = await get_camera_backend_token(current_user)
    # 若沒有自己的 token，嘗試用 camera_access granter 的 token
    if not cam_token and access and access.granted_by:
        owner = db.query(User).filter(User.id == access.granted_by).first()
        if owner:
            cam_token = await get_camera_backend_token(owner)
    # 最後 fallback 到 admin token（僅 grant/admin）
    if not cam_token and allow_fallback:
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
    # user token 被拒（相機屬於不同 CB 帳號）：F-13 先試 granter token，再退 admin（僅 grant/admin）
    if resp.status_code in (403, 404) and allow_fallback:
        gtok = await _try_granter_token(camera_id, access, current_user, db)
        if gtok:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.request(
                    method=request.method,
                    url=f"{CAMERA_BACKEND_URL}/api/cameras/{camera_id}/{path}",
                    headers={"Authorization": f"Bearer {gtok}", "Content-Type": "application/json"},
                    content=body,
                    params=dict(request.query_params),
                )
    if resp.status_code in (403, 404) and allow_fallback:
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



@router.post("/{camera_id}/prepare-timelapse")
async def prepare_timelapse_folder(
    camera_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """建立每天均勻取樣的縮時暫存資料夾，供 Spark /jobs/nas 使用"""
    import shutil, math
    from datetime import datetime as dt

    body = await request.json()
    serial_id: str = body.get("serial_id", "")
    start_date: str = body.get("start_date", "")   # YYYY-MM-DD
    end_date: str = body.get("end_date", "")
    target_secs: int = int(body.get("target_duration_secs", 0))  # 0 = 不限
    fps: int = int(body.get("fps", 30))

    if not serial_id:
        raise HTTPException(400, "serial_id 必填")

    nas_base = f"/homes/firmness/{serial_id}"
    if not os.path.isdir(nas_base):
        raise HTTPException(404, f"找不到 NAS 資料夾：{nas_base}")

    # 1. 列出日期子資料夾（YYYY-MM-DD 格式），依日期過濾
    def valid_date(d):
        try: dt.strptime(d, "%Y-%m-%d"); return True
        except: return False

    all_date_dirs = sorted([
        d for d in os.listdir(nas_base)
        if valid_date(d) and os.path.isdir(os.path.join(nas_base, d))
    ])

    if start_date: all_date_dirs = [d for d in all_date_dirs if d >= start_date]
    if end_date:   all_date_dirs = [d for d in all_date_dirs if d <= end_date]

    if not all_date_dirs:
        raise HTTPException(404, "指定範圍內沒有照片")

    # 2. 收集每天的照片列表
    image_exts = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}
    def list_images(date_dir):
        dpath = os.path.join(nas_base, date_dir)
        files = sorted([
            f for f in os.listdir(dpath)
            if os.path.splitext(f)[1].lower() in image_exts
        ])
        return [(date_dir, f) for f in files]

    photos_by_day = {d: list_images(d) for d in all_date_dirs}
    total_photos = sum(len(v) for v in photos_by_day.values())
    num_days = len(all_date_dirs)

    # 3. 計算每天分配幾 frames
    if target_secs > 0:
        total_frames = target_secs * fps
        frames_per_day = max(1, total_frames // num_days)
    else:
        # 不限制 → 直接用原始 nas_path，不建暫存
        return {
            "nas_folder": serial_id,
            "total_photos": total_photos,
            "sampled_photos": total_photos,
            "days": num_days,
            "estimated_secs": total_photos // fps,
            "temp_created": False,
        }

    # 4. 每天均勻取樣
    sampled: list[tuple[str, str]] = []
    for date_dir, photos in photos_by_day.items():
        if len(photos) <= frames_per_day:
            sampled.extend(photos)
        else:
            step = len(photos) / frames_per_day
            sampled.extend(photos[int(i * step)] for i in range(frames_per_day))

    # 5. 建暫存資料夾，複製取樣照片（重新命名確保時間順序）
    import time as _time
    job_token = f"tl_{camera_id}_{int(_time.time())}"
    temp_dir = f"/homes/firmness/{job_token}"
    os.makedirs(temp_dir, exist_ok=True)

    try:
        for idx, (date_dir, fname) in enumerate(sampled):
            src = os.path.join(nas_base, date_dir, fname)
            ext = os.path.splitext(fname)[1]
            dst = os.path.join(temp_dir, f"{idx:06d}{ext}")
            if os.path.exists(src):
                os.link(src, dst)  # hardlink 省空間
    except OSError:
        # hardlink 失敗（跨裝置）→ 用 symlink
        shutil.rmtree(temp_dir, ignore_errors=True)
        os.makedirs(temp_dir, exist_ok=True)
        for idx, (date_dir, fname) in enumerate(sampled):
            src = os.path.join(nas_base, date_dir, fname)
            ext = os.path.splitext(fname)[1]
            dst = os.path.join(temp_dir, f"{idx:06d}{ext}")
            if os.path.exists(src):
                os.symlink(src, dst)

    return {
        "nas_folder": job_token,
        "total_photos": total_photos,
        "sampled_photos": len(sampled),
        "days": num_days,
        "estimated_secs": len(sampled) // fps,
        "temp_created": True,
    }
