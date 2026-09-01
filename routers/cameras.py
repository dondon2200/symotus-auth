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
import time as _time
import httpx

from datetime import datetime
from database import get_db
from models import User, CameraAccess, TechSupportGrant
from auth import get_current_user, to_backend_role
from audit import log_action
from policies import level_allows, feature_for_write

router = APIRouter(prefix="/cameras", tags=["cameras"])

CAMERA_BACKEND_URL = "https://user.symotus.com"
import os
CAMERA_SERVICE_KEY = os.environ.get("CAMERA_SERVICE_KEY", "")

logger = logging.getLogger(__name__)


# Camera Backend token 快取：(camera_email, backend_role) → (token, expires_at)
# 緣由：每張圖片代理都會換一次 token，公開頁縮時預載一次要抓上百張，
# 等量的 token 簽發打爆連線池，服務會被拖到 unhealthy（2026-08-14 事故）。
_CAM_TOKEN_TTL = 300.0
_cam_token_cache: dict[tuple[str, str], tuple[str, float]] = {}


async def get_camera_backend_token(user: User) -> str:
    """取得 Camera Backend token（同帳號 5 分鐘內共用同一顆）
    安全原則：
    - 必須有 camera_email 才能換 token（代表該帳號有在 Camera Backend 配對過相機）
    - LINE 自動合成的 camera_email（line_xxx@symotus.com）Camera Backend 會自動建立帳號，正常換 token
    - 沒有 camera_email 的用戶無法直接存取 Camera Backend，只能透過 camera_access 看授權相機
    - 快取鍵含 role：同帳號換角色不會拿到舊權限的 token
    """
    if not user.camera_email:
        return ""  # 沒有 camera_email = 沒有 Camera Backend 帳號，不給 token

    key = (user.camera_email, to_backend_role(user.role))
    now = _time.monotonic()
    hit = _cam_token_cache.get(key)
    if hit and hit[1] > now:
        return hit[0]

    # user_id=0 讓 Camera Backend 純用 email 查帳號，避免 user_id 不一致問題
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{CAMERA_BACKEND_URL}/internal/auth/token",
            headers={"x-service-key": CAMERA_SERVICE_KEY},
            json={"user_id": 0, "email": user.camera_email, "role": to_backend_role(user.role)},
        )
        if resp.status_code == 200:
            token = resp.json().get("access_token", "")
            if token:
                _cam_token_cache[key] = (token, now + _CAM_TOKEN_TTL)
                # 順手清掉過期項，避免長期累積
                for k in [k for k, (_, exp) in _cam_token_cache.items() if exp <= now]:
                    _cam_token_cache.pop(k, None)
            return token
    return ""


def get_allowed_camera_ids(user: User, db: Session) -> Optional[list[int]]:
    """
    取得用戶可存取的 camera_id 列表
    - symotus_admin: None（不限制，Camera Backend 自己管）
    - reseller/end_user: 只能看 camera_access 表裡授權的相機（範圍由 camera_access 統一控管；
      reseller 配對相機時 create_camera 會自動建對應 grant，見下方）
    """
    if user.role == "symotus_admin":
        return None  # 不限制
    # reseller/end_user 只能看被授權的相機
    accesses = db.query(CameraAccess).filter(CameraAccess.user_id == user.id).all()
    return [a.camera_id for a in accesses]


async def _get_admin_camera_token(user_id: int = 0) -> str:
    """取得 Camera Backend 的 admin 備用 token（granted_by 非真正擁有者時 fallback 用）。

    user_id 預設 0（讀取路徑沿用既有行為不變）。camera-delete-backend-500 雷區：
    對 CB 的寫入操作（DELETE 等）若帶假造 user_id=0 的 token 會 500——admin@timelapse.com
    在 Camera Backend 的真實 user_id 需由呼叫端（如 DELETE /cameras/{id}）查 DB
    User.camera_user_id 後傳入，避免手造 user_id=0 做寫入。"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{CAMERA_BACKEND_URL}/internal/auth/token",
                headers={"x-service-key": CAMERA_SERVICE_KEY},
                json={"user_id": user_id, "email": "admin@timelapse.com", "role": "admin"},
            )
            return r.json().get("access_token", "") if r.status_code == 200 else ""
    except Exception as e:
        logger.warning("admin fallback token 取得失敗: %s", e)
        return ""


async def _admin_camera_project_map(admin_tok: str) -> dict[int, set[int]]:
    """用 admin token 取得完整相機清單，回傳 {project_id: {camera_id, ...}}。

    緣由：CB 的 `GET /api/projects`（ProjectResponse）不含相機清單，只能反查
    `GET /api/cameras` 各相機的 `project_id` 欄位來重建 project→camera 對應，
    供 timer-status/projects 的靜默降級回應過濾、與 DELETE project 的歸屬檢查共用。
    受 `/api/cameras` 分頁限制（單次抓 1000 筆），相機總數若超過此上限會有遺漏風險。"""
    mapping: dict[int, set[int]] = {}
    if not admin_tok:
        return mapping
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{CAMERA_BACKEND_URL}/api/cameras",
                headers={"Authorization": f"Bearer {admin_tok}"},
                params={"limit": 1000},
            )
        if resp.status_code == 200:
            for c in resp.json().get("cameras", []):
                pid, cid = c.get("project_id"), c.get("id")
                if pid is not None and cid is not None:
                    mapping.setdefault(pid, set()).add(cid)
    except Exception as e:
        logger.warning("admin_camera_project_map 取得失敗: %s", e)
    return mapping


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
                cam = raw.get("basic_info", raw)  # 攤平 detail 格式
                # 補抓電量資料（basic_info 不含電量欄位，需從 /battery 取得）
                try:
                    async with httpx.AsyncClient(timeout=5) as bc:
                        br = await bc.get(
                            f"{CAMERA_BACKEND_URL}/api/cameras/{camera_id}/battery",
                            headers={"Authorization": f"Bearer {token}"},
                        )
                    if br.status_code == 200:
                        battery_data = br.json()
                        for bk in ("last_battery_pct", "last_battery_status", "battery_updated_at"):
                            if bk in battery_data:
                                cam[bk] = battery_data[bk]
                except Exception as be:
                    logger.debug("fetch battery %s via %s token error: %s", camera_id, label, be)
                return cam
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
    allowed_ids = get_allowed_camera_ids(current_user, db)
    cam_token = await get_camera_backend_token(current_user)
    if not cam_token:
        # 無自有 token（如 reseller 未設 camera_email）：退回 admin token，
        # 下方仍會依 allowed_ids 過濾，不會洩漏非授權相機的排程。
        cam_token = await _get_admin_camera_token()
    if not cam_token:
        return {"timers": []}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{CAMERA_BACKEND_URL}/api/cameras/timer-status",
            headers={"Authorization": f"Bearer {cam_token}"},
        )
    if resp.status_code == 200:
        data = resp.json()
        if allowed_ids is not None and isinstance(data, dict) and isinstance(data.get("cameras"), list):
            allowed_set = set(allowed_ids)
            data["cameras"] = [c for c in data["cameras"] if c.get("camera_id") in allowed_set]
            data["total"] = len(data["cameras"])
        return data
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
    # 先用自己的 backend token 拿；拿得到的是自己 backend 帳號擁有的相機
    result: dict = {}
    if cam_token:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{CAMERA_BACKEND_URL}/api/cameras/thumbnails/latest",
                headers={"Authorization": f"Bearer {cam_token}"},
                params={"ids": ",".join(str(i) for i in requested_ids)},
            )
        if resp.status_code == 200 and resp.content:
            try:
                data = resp.json()
                if isinstance(data, dict):
                    result = data
            except ValueError:
                pass

    # Camera Backend 依 backend 帳號擁有權過濾：自己 token 的 200 回應會「缺漏」
    # 不屬於自己的相機（如 LINE 帳號，相機綁在 admin 名下、僅靠 camera_access 授權），
    # 所以必須逐 id 補查，不能只在整個請求失敗時才 fallback。
    missing = [i for i in requested_ids if str(i) not in result]
    if missing:
        # F-3：非 admin 僅對「有 camera_access grant 的相機」允許 admin fallback
        if current_user.role == "symotus_admin":
            fb_ids = missing
        else:
            granted = {a.camera_id for a in db.query(CameraAccess).filter(
                CameraAccess.user_id == current_user.id).all()}
            fb_ids = [i for i in missing if i in granted]
        if fb_ids:
            admin_tok = await _get_admin_camera_token()
            if admin_tok:
                async with httpx.AsyncClient(timeout=15) as client:
                    fb_resp = await client.get(
                        f"{CAMERA_BACKEND_URL}/api/cameras/thumbnails/latest",
                        headers={"Authorization": f"Bearer {admin_tok}"},
                        params={"ids": ",".join(str(i) for i in fb_ids)},
                    )
                if fb_resp.status_code == 200 and fb_resp.content:
                    try:
                        fb_data = fb_resp.json()
                        if isinstance(fb_data, dict):
                            for k, v in fb_data.items():
                                result.setdefault(k, v)
                    except ValueError:
                        pass
    return result


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
        cam_token = await _get_admin_camera_token()
    if not cam_token:
        raise HTTPException(502, "無法取得 Camera Backend token，請確認 camera_email 設定")
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

    # 自動幫 reseller 建立 camera_access（full 權限）：
    # get_allowed_camera_ids 現在對 reseller 也是靠 camera_access 控管範圍，
    # 所以無論有沒有自己的 camera_email（有自有 token 或走 admin fallback），
    # 配對成功都要建這筆 grant，否則自己剛配的相機在 list_cameras 等端點會直接消失。
    if resp.status_code in (200, 201) and current_user.role == "reseller":
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
                    invitation_id=0,  # 非邀請來源（哨兵，避免撤銷連結時 NULL fallback 誤刪）
                ))
                db.commit()

    return JSONResponse(status_code=resp.status_code, content=resp_data)


@router.get("/{camera_id:int}")  # :int 轉換器，避免 /projects 等字面路徑被當成 camera_id 而 422
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
        cam_token = await _get_admin_camera_token()
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
        admin_token = await _get_admin_camera_token()
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
    # 先查 camera_access（分享邀請授權）；僅「真分享」列（granted_by 非本人）以授權等級為準。
    # 自我配對列（訂閱通知自動建立）不是被分享，D6 改版後若誤標會讓擁有者在
    # 自己相機上失去串流/通知 UI——視同擁有者處理，不回其列上的等級。
    # access 已在上方查過
    if access and access.granted_by != current_user.id:
        data["my_permission"] = access.permission_level or "photos_stream"
    elif current_user.role in ("reseller", "symotus_admin"):
        data["my_permission"] = "full"  # 自己擁有的相機
    else:
        data["my_permission"] = "stream_only"

    return data


# ── 開機通知共用邏輯 ──────────────────────────────────────────────────────────
async def _is_following_oa(line_id: str) -> bool:
    """檢查用戶是否追蹤官方帳號。LINE API 逾時/錯誤時降級放行（回 True），
    只有明確 404 才判定未追蹤，避免暫時性失敗誤擋既有好友的訂閱（0-b）。"""
    token = os.environ.get("LINE_ACCESS_TOKEN", "")
    if not token:
        return True
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(
                f"https://api.line.me/v2/bot/profile/{line_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        return r.status_code != 404
    except Exception:
        return True


def _set_notify(db: Session, camera_id: int, user: User, value: bool) -> None:
    """設定某用戶對某相機的開機通知；更新所有符合列，無列則建一列（0-c/0-d）。
    建列讓 admin（原本無 camera_access 列、以全域權限收通知）也能留下退訂標記。

    C-1 修正：非 admin 且查無既有列時「不」建列——否則等於讓呼叫者對任意
    camera_id 憑空造出一筆 camera_access grant（自我授權旁路）。非 admin 無列
    本來就不會收到通知，退訂在這種情況下是 no-op。"""
    rows = db.query(CameraAccess).filter(
        CameraAccess.camera_id == camera_id,
        CameraAccess.user_id == user.id,
    ).all()
    if rows:
        for acc in rows:
            acc.notify_on_online = value
    elif user.role == "symotus_admin":
        db.add(CameraAccess(
            camera_id=camera_id, user_id=user.id,
            granted_by=user.id, permission_level="stream_only",
            notify_on_online=value,
            invitation_id=0,  # 非邀請來源（哨兵，避免撤銷連結時 NULL fallback 誤刪）
        ))


def _is_subscribed(db: Session, camera_id: int, user: User) -> bool:
    """判斷此用戶目前是否會收到該相機的開機通知。
    admin 預設會收（除非有明確退訂列）；其餘角色需有 notify_on_online=True 的列。"""
    rows = db.query(CameraAccess).filter(
        CameraAccess.camera_id == camera_id,
        CameraAccess.user_id == user.id,
    ).all()
    has_true = any(getattr(a, "notify_on_online", False) for a in rows)
    has_false = any(getattr(a, "notify_on_online", True) is False for a in rows)
    if user.role == "symotus_admin":
        return has_true or not has_false
    return has_true


@router.get("/{camera_id}/live-frame-url")
async def get_live_frame_url(
    camera_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """回傳簽章過的公開即時截圖 URL（給 AI 助理/LINE 用，30 分鐘有效）
    供「看一下畫面」這類需要真正即時畫面（非 NAS 歷史照片）的請求使用
    """
    import time as _time
    from routers.public_camera import _live_frame_sig

    allowed_ids = get_allowed_camera_ids(current_user, db)
    if allowed_ids is not None and camera_id not in allowed_ids:
        raise HTTPException(403, "無此相機的存取權限")

    exp = int(_time.time()) + 1800
    sig = _live_frame_sig(camera_id, exp)
    frontend_url = os.getenv("FRONTEND_URL", "https://admin.symotus.com")
    url = f"{frontend_url}/auth-api/cameras/public/live-frame/{camera_id}?exp={exp}&sig={sig}"
    return {"url": url}


@router.post("/{camera_id}/notify-subscribe")
async def subscribe_online_notification(
    camera_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """訂閱相機開機 LINE 通知"""
    allowed_ids = get_allowed_camera_ids(current_user, db)
    if allowed_ids is not None and camera_id not in allowed_ids:
        raise HTTPException(403, "無此相機的存取權限")

    # notify.subscribe 政策：被分享者（非自我配對）等級不足或功能停用 → 拒訂（退訂不受限）
    _acc = db.query(CameraAccess).filter(
        CameraAccess.camera_id == camera_id, CameraAccess.user_id == current_user.id,
    ).first()
    if (_acc and _acc.granted_by != current_user.id
            and current_user.role != "symotus_admin"
            and not level_allows(db, "notify.subscribe", _acc.permission_level)):
        raise HTTPException(403, "此操作需要更高的授權等級（notify.subscribe）")

    line_accounts = current_user.line_accounts
    if not line_accounts:
        return {"subscribed": False, "needs_line": True, "is_following": False,
                "message": "請先綁定 LINE 帳號"}
    if not await _is_following_oa(line_accounts[0].line_user_id):
        return {"subscribed": False, "needs_line": True, "is_following": False,
                "message": "請先加入官方 LINE 帳號以接收通知"}

    _set_notify(db, camera_id, current_user, True)
    db.commit()
    return {"subscribed": True, "is_following": True, "message": "開機時將透過 LINE 通知您"}


@router.post("/{camera_id}/notify-unsubscribe")
async def unsubscribe_online_notification(
    camera_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """取消相機開機 LINE 通知"""
    allowed_ids = get_allowed_camera_ids(current_user, db)
    if allowed_ids is not None and camera_id not in allowed_ids:
        raise HTTPException(403, "無此相機的存取權限")

    _set_notify(db, camera_id, current_user, False)
    db.commit()
    return {"subscribed": False, "message": "已取消開機通知"}


@router.get("/{camera_id}/notify-status")
async def get_notify_status(
    camera_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """查詢此相機的通知訂閱狀態"""
    if not current_user.line_accounts:
        return {"subscribed": False, "needs_line": True}
    return {"subscribed": _is_subscribed(db, camera_id, current_user)}


@router.get("/notify-settings")
async def notify_settings(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """集中式通知設定：回傳此用戶已訂閱 / 已退訂的相機清單（P1）。
    相機名稱由前端從 /cameras 取得，這裡只回訂閱狀態，避免重複打 Camera Backend。"""
    if not current_user.line_accounts:
        return {"needs_line": True, "role": current_user.role, "subscribed": [], "suppressed": []}
    rows = db.query(CameraAccess).filter(CameraAccess.user_id == current_user.id).all()
    subscribed = sorted({a.camera_id for a in rows if getattr(a, "notify_on_online", False)})
    suppressed = sorted({a.camera_id for a in rows
                         if getattr(a, "notify_on_online", True) is False} - set(subscribed))
    return {"needs_line": False, "role": current_user.role,
            "subscribed": subscribed, "suppressed": suppressed}


@router.post("/notify-bulk")
async def notify_bulk(
    body: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """批量開/關開機通知（P1「一鍵全開/全關」）。
    body: {"subscribe": bool, "camera_ids": [int, ...]}。
    camera_ids 由前端帶入（通常為畫面上可見的相機）；會再以權限過濾。"""
    subscribe = bool(body.get("subscribe", True))
    camera_ids = [int(c) for c in (body.get("camera_ids") or [])]
    allowed_ids = get_allowed_camera_ids(current_user, db)
    if allowed_ids is not None:
        allowed_set = set(allowed_ids)
        camera_ids = [c for c in camera_ids if c in allowed_set]

    if subscribe:
        line_accounts = current_user.line_accounts
        if not line_accounts:
            return {"needs_line": True, "message": "請先綁定 LINE 帳號"}
        if not await _is_following_oa(line_accounts[0].line_user_id):
            return {"needs_line": True, "message": "請先加入官方 LINE 帳號以接收通知"}
        # notify.subscribe 政策：略過等級不足的被分享相機（退訂不受限）
        if current_user.role != "symotus_admin":
            shared = {a.camera_id: a for a in db.query(CameraAccess).filter(
                CameraAccess.user_id == current_user.id,
                CameraAccess.granted_by != current_user.id,
            ).all()}
            camera_ids = [c for c in camera_ids
                          if c not in shared or level_allows(db, "notify.subscribe", shared[c].permission_level)]

    for cam_id in camera_ids:
        _set_notify(db, cam_id, current_user, subscribe)
    db.commit()

    rows = db.query(CameraAccess).filter(CameraAccess.user_id == current_user.id).all()
    result = sorted({a.camera_id for a in rows if getattr(a, "notify_on_online", False)})
    return {"ok": True, "subscribed": result, "count": len(camera_ids)}


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


@router.delete("/{camera_id:int}")  # 隱藏的永久刪除（前端只在 ?joseph 時顯示按鈕）
async def delete_camera(
    camera_id: int,
    confirm: bool = False,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    永久刪除相機（破壞性，無法復原）。
    安全閘：
    1. 擁有者（reseller / symotus_admin，end_user 一律拒）；被分享者需政策允許（D5：預設
       full 授權）才能刪除，其餘等級（photos_stream/stream_only）一律拒。
       非 admin（reseller）的「擁有者路徑」現在還要求真的持有這台相機的 CameraAccess
       記錄，不再是「角色是 reseller 就放行任意 camera_id」（Task 3：補 admin fallback
       同時把可寫範圍收斂到 camera_access 為界）。symotus_admin 維持現行不受此限制。
    2. 需 ?confirm=true，防誤觸。
    3. token 依序：自己的真實 Camera Backend token → 借 granter token（F-13，排除自我
       授權）→ admin fallback（涵蓋無 camera_email 但持自我 grant 的擁有者）。
       admin fallback 為正式簽發 token，理論可用；仍需留意 camera-delete-backend-500
       雷區（歷史上 500 疑似源自假 user_id=0 token，非 cascade bug，真 token 沒事）——
       部署後請以此路徑實測驗證。
    4. 刪除／換機皆記 audit log（定案③配套：刪除權隨授權鏈外擴，必留稽核）。
    """
    if not confirm:
        raise HTTPException(400, "需帶 ?confirm=true 才會真正刪除")

    access = db.query(CameraAccess).filter(
        CameraAccess.camera_id == camera_id,
        CameraAccess.user_id == current_user.id,
    ).first()
    shared = bool(access and access.granted_by and access.granted_by != current_user.id)

    # D5：被分享者需政策允許（預設 full）才能刪；擁有者路徑 reseller 現在要求持有 grant，
    # symotus_admin 維持現行（不受此限制）
    if shared:
        if not level_allows(db, "camera.delete", access.permission_level):
            raise HTTPException(403, "此相機為他人分享，需「全功能管理」授權才能刪除")
    elif current_user.role == "reseller":
        if access is None:
            raise HTTPException(403, "無此相機的存取權限")
    elif current_user.role != "symotus_admin":
        raise HTTPException(403, "僅系統管理員或相機擁有者可刪除相機")

    # token：自己的 → 借 granter token（F-13，排除自我授權）→ admin fallback
    cam_token = await get_camera_backend_token(current_user)
    if not cam_token:
        cam_token = await _try_granter_token(camera_id, access, current_user, db)
    if not cam_token:
        # camera-delete-backend-500 雷區：DELETE 對 CB 是寫入操作，user_id=0 的假造 token
        # 會 500。查 admin@timelapse.com 在 CB 的真實 camera_user_id（無值 fallback 1）。
        admin_user = db.query(User).filter(User.camera_email == "admin@timelapse.com").first()
        admin_camera_user_id = (admin_user.camera_user_id if admin_user and admin_user.camera_user_id else 1)
        cam_token = await _get_admin_camera_token(user_id=admin_camera_user_id)
    if not cam_token:
        raise HTTPException(403, "無法取得可刪除此相機的有效憑證")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.delete(
            f"{CAMERA_BACKEND_URL}/api/cameras/{camera_id}",
            headers={"Authorization": f"Bearer {cam_token}"},
        )
    logger.warning(
        "CAMERA_DELETE user=%s role=%s camera=%s status=%s",
        current_user.id, current_user.role, camera_id, resp.status_code,
    )
    if resp.status_code not in (200, 204):
        raise HTTPException(resp.status_code, resp.text)

    log_action(db, current_user, "camera.delete", "camera", camera_id,
               f"shared={shared} granter={access.granted_by if shared else '-'} status={resp.status_code}")
    db.commit()

    # 清掉所有殘留的 camera_access，避免死記錄
    db.query(CameraAccess).filter(CameraAccess.camera_id == camera_id).delete()
    db.commit()
    return {"success": True, "camera_id": camera_id}


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

    # photos.view 政策：被分享者（非自我配對）等級不足 → 後端直接擋（不再只靠 UI 藏按鈕）
    if (access and access.granted_by != current_user.id
            and current_user.role != "symotus_admin"
            and not level_allows(db, "photos.view", access.permission_level)):
        raise HTTPException(403, "此操作需要更高的授權等級（photos.view）")

    # 沒有自己的 token（分享用戶）→ 用 granter 的 token
    if not cam_token and access and access.granted_by:
        owner = db.query(User).filter(User.id == access.granted_by).first()
        if owner:
            cam_token = await get_camera_backend_token(owner)

    # 仍無 token 且有 grant/admin：admin fallback（涵蓋自我 grant 但無 camera_email 的擁有者）
    if not cam_token and allow_fallback:
        cam_token = await _get_admin_camera_token()

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
                adm_tok = await _get_admin_camera_token()
                if adm_tok:
                    cam_token = adm_tok

    return await list_nas_images_backend(cam_token, camera_id, params)


async def list_nas_images_backend(cam_token: str, camera_id, params: dict):
    """共用 NAS 照片列表核心：解析 serial → 掃日期資料夾 → 分頁蒐集照片。
    供登入版 /cameras/nas/images 與公開分享 /cameras/public/{token}/images 共用；
    權限與 token 由呼叫端負責，camera_id 必須由呼叫端決定（公開端點強制取自邀請）。"""
    from datetime import timedelta

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
                    # 從 image_url 解析時間（檔名格式通常為 HHMMSS.jpg）
                    if "image_url" in f and not f.get("taken_at"):
                        try:
                            import re as _re
                            img = f["image_url"]
                            # 嘗試從路徑或 query string 中取出 6 位數字
                            m = _re.search(r"[/=](\d{6})\.jpe?g", img, _re.IGNORECASE)
                            if not m:
                                m = _re.search(r"(\d{6})\.jpe?g", img, _re.IGNORECASE)
                            if m:
                                t = m.group(1)
                                hh, mm, ss = int(t[0:2]), int(t[2:4]), int(t[4:6])
                                if hh < 24 and mm < 60 and ss < 60:
                                    f["taken_at"] = f"{date_str}T{t[0:2]}:{t[2:4]}:{t[4:6]}"
                        except Exception:
                            pass
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
        # 路徑格式：/homes/firmness/{serial}/... 無法直接得知 camera_id
        # D3：縮圖（相簿瀏覽/預覽）走 photos.view；原圖走 photos.download。
        # 「不可下載」的伺服器層強制點在此——stream_only 被分享者抓原圖 403。
        is_thumb = str(request.query_params.get("thumbnail", "")).lower() in ("1", "true", "yes")
        feature = "photos.view" if is_thumb else "photos.download"
        accesses = db.query(CameraAccess).filter(CameraAccess.user_id == current_user.id).all()
        viewable = [a for a in accesses
                    if a.granted_by == current_user.id
                    or level_allows(db, feature, a.permission_level)]
        if accesses and not viewable and current_user.role != "symotus_admin":
            raise HTTPException(403, f"此操作需要更高的授權等級（{feature}）")
        for a in viewable:
            if a.granted_by and a.granted_by != current_user.id:
                owner = db.query(User).filter(User.id == a.granted_by).first()
                if owner:
                    cam_token = await get_camera_backend_token(owner)
                    if cam_token:
                        break
        # 仍無 token 且（本人持有可視 grant，或本身是 admin）：admin fallback
        if not cam_token and (viewable or current_user.role == "symotus_admin"):
            cam_token = await _get_admin_camera_token()
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


@router.delete("/projects/{project_id}")
async def delete_project(
    project_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.role not in ("reseller", "symotus_admin"):
        raise HTTPException(403, "沒有刪除專案的權限")
    if current_user.role != "symotus_admin":
        # 歸屬檢查：project 內至少一台相機在自己的 allowed_ids 才准刪，避免 admin
        # fallback token 被用來刪除不相干 reseller 的 project。查不到 project 內容
        # （admin token 拿不到、或 project 底下查無相機）一律保守 403。
        allowed_ids = set(get_allowed_camera_ids(current_user, db) or [])
        pmap = await _admin_camera_project_map(await _get_admin_camera_token())
        if not (pmap.get(project_id, set()) & allowed_ids):
            raise HTTPException(403, "此專案不在你的相機授權範圍")
    cam_token = await get_camera_backend_token(current_user)
    if not cam_token:
        # camera-delete-backend-500 雷區：project DELETE 對 CB 也是寫入操作，user_id=0
        # 的假造 token 會 500。查 admin@timelapse.com 在 CB 的真實 camera_user_id
        # （無值 fallback 1），與 DELETE /cameras/{id} 同一套處理。
        admin_user = db.query(User).filter(User.camera_email == "admin@timelapse.com").first()
        admin_camera_user_id = (admin_user.camera_user_id if admin_user and admin_user.camera_user_id else 1)
        cam_token = await _get_admin_camera_token(user_id=admin_camera_user_id)
    if not cam_token:
        raise HTTPException(502, "無法取得 Camera Backend token")
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.delete(
            f"{CAMERA_BACKEND_URL}/api/projects/{project_id}",
            headers={"Authorization": f"Bearer {cam_token}"},
        )
        try:
            return JSONResponse(status_code=resp.status_code, content=resp.json() if resp.content else {})
        except Exception:
            return JSONResponse(status_code=resp.status_code, content={"detail": resp.text})


@router.api_route("/{camera_id:int}", methods=["PUT", "PATCH"])  # bare /cameras/{id} 寫入（如改名）
@router.api_route("/{camera_id}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_camera_api(
    camera_id: int,
    request: Request,
    path: str = "",
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    通用 proxy：所有其他相機 API（設定、排程等）
    先驗證權限，再轉發到 Camera Backend
    path 為空時轉發到 /api/cameras/{id} 本體（如更新相機名稱）。
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

    # F-5：寫入類操作依 FeaturePolicy 政策檢查（預設＝原「需 full」行為）。
    # 只約束「真正的被分享者」：自我配對列（granted_by==自己，如訂閱通知時自動建立的
    # stream_only 列）視同擁有者，不降權；symotus_admin 一律豁免。
    # 路徑分流：ptz/reboot/autofocus→camera.control、空路徑→camera.rename、其餘→camera.settings。
    if (request.method not in ("GET", "HEAD") and access
            and access.granted_by != current_user.id
            and current_user.role != "symotus_admin"):
        feature = feature_for_write(path)
        if not level_allows(db, feature, access.permission_level):
            raise HTTPException(403, f"此操作需要更高的授權等級（{feature}）")
        if feature == "device.replace":
            # 定案③配套：被分享者執行換機/撤機必留稽核
            log_action(db, current_user, "device.replace", "camera", camera_id,
                       f"path={path} via_grant_from={access.granted_by}")
            db.commit()

    # 縮時影片下載走 spark 代理無認證，故在列表端阻斷 job id 洩漏（D3）：
    # 低等級真被分享者連 timelapse-jobs 清單（含 job id/下載連結）都不可讀。
    if (request.method == "GET" and access
            and access.granted_by != current_user.id
            and current_user.role != "symotus_admin"
            and (path or "").split("/")[0].lower().startswith("timelapse-jobs")):
        if not level_allows(db, "photos.download", access.permission_level):
            raise HTTPException(403, "此操作需要更高的授權等級（photos.download）")

    # A3 決策（2026-07-27）：admin 不受 TechSupportGrant 限制——它是「同意記錄」而非閘門。
    # 落地方式：admin 的寫入操作若未被任何有效支援授權涵蓋，寫一筆稽核（不阻擋），
    # 讓「未經同意動了誰的相機」永遠可追溯。有涵蓋（明列相機或全相機授權）則視為已同意、不記。
    if request.method not in ("GET", "HEAD") and current_user.role == "symotus_admin":
        now = datetime.utcnow()
        grants = db.query(TechSupportGrant).filter(
            TechSupportGrant.expires_at > now,
            TechSupportGrant.revoked_at == None,  # noqa: E711
        ).all()
        covered = any(g.camera_ids is None or camera_id in (g.camera_ids or []) for g in grants)
        if not covered:
            log_action(db, current_user, "admin_write_no_grant", "camera", camera_id,
                       f"path={path or '(rename)'} method={request.method}")
            db.commit()

    cam_token = await get_camera_backend_token(current_user)
    # 若沒有自己的 token，嘗試用 camera_access granter 的 token
    if not cam_token and access and access.granted_by:
        owner = db.query(User).filter(User.id == access.granted_by).first()
        if owner:
            cam_token = await get_camera_backend_token(owner)
    # 最後 fallback 到 admin token（僅 grant/admin）
    if not cam_token and allow_fallback:
        cam_token = await _get_admin_camera_token()
    body = await request.body()
    headers = {"Authorization": f"Bearer {cam_token}", "Content-Type": "application/json"}
    target_url = f"{CAMERA_BACKEND_URL}/api/cameras/{camera_id}" + (f"/{path}" if path else "")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.request(
            method=request.method,
            url=target_url,
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
                    url=target_url,
                    headers={"Authorization": f"Bearer {gtok}", "Content-Type": "application/json"},
                    content=body,
                    params=dict(request.query_params),
                )
    if resp.status_code in (403, 404) and allow_fallback:
        admin_tok = await _get_admin_camera_token()
        if admin_tok:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.request(
                    method=request.method,
                    url=target_url,
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
    allowed_ids = get_allowed_camera_ids(current_user, db)
    cam_token = await get_camera_backend_token(current_user)
    if not cam_token:
        # 無自有 token：退回 admin token，下方仍依 allowed_ids 過濾 project 清單。
        cam_token = await _get_admin_camera_token()
    if not cam_token:
        return {"projects": []}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{CAMERA_BACKEND_URL}/api/projects",
            headers={"Authorization": f"Bearer {cam_token}"},
        )
        if resp.status_code == 200:
            data = resp.json()
            if allowed_ids is not None and isinstance(data, dict) and isinstance(data.get("projects"), list):
                # ProjectResponse 不含相機清單，反查 project→camera 對應後只留「至少
                # 一台相機在 allowed_ids」的 project；admin token 另抓一次（cam_token
                # 可能是 reseller 自己的 token，不能拿來查全量相機）。
                admin_tok = await _get_admin_camera_token()
                pmap = await _admin_camera_project_map(admin_tok)
                allowed_set = set(allowed_ids)
                data["projects"] = [
                    p for p in data["projects"] if pmap.get(p.get("id"), set()) & allowed_set
                ]
                data["total"] = len(data["projects"])
            return data
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
        cam_token = await _get_admin_camera_token()
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
