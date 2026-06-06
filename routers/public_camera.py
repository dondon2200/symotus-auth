"""
公開相機存取（無需登入）
用於「有連結即可看串流＋縮時預覽」的分享功能
"""
import httpx
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from database import get_db
from models import User, CameraInvitation
from routers.cameras import get_camera_backend_token, CAMERA_BACKEND_URL

router = APIRouter(prefix="/cameras/public", tags=["public-camera"])


async def _get_public_cam(token: str, db: Session):
    """共用：驗證公開 token，回傳 (invitation, granter_token)"""
    inv = db.query(CameraInvitation).filter(
        CameraInvitation.token == token,
        CameraInvitation.is_public == True,
    ).first()
    if not inv:
        raise HTTPException(404, "連結無效")
    if inv.expires_at and inv.expires_at < datetime.utcnow():
        raise HTTPException(410, "連結已過期")

    granter = db.query(User).filter(User.id == inv.inviter_id).first()
    if not granter:
        raise HTTPException(500, "找不到分享者")

    cam_token = await get_camera_backend_token(granter)
    if not cam_token:
        raise HTTPException(502, "無法取得相機存取權")

    return inv, cam_token


@router.get("/{token}")
async def get_public_camera_info(token: str, db: Session = Depends(get_db)):
    """取得公開分享相機資訊（串流 stream_name + camera_name）"""
    inv, cam_token = await _get_public_cam(token, db)

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{CAMERA_BACKEND_URL}/api/cameras/{inv.camera_id}",
            headers={"Authorization": f"Bearer {cam_token}"},
        )

    if resp.status_code != 200:
        raise HTTPException(502, "無法取得相機資訊")

    cam = resp.json()
    basic = cam.get("basic_info", cam)
    ip = basic.get("ip_address", "")
    stream_name = f"cam{ip.split('.')[2]}" if ip and len(ip.split('.')) >= 3 else ""

    return {
        "camera_id": inv.camera_id,
        "camera_name": inv.camera_name or basic.get("name", f"相機 #{inv.camera_id}"),
        "ip_address": ip,
        "stream_name": stream_name,
        "online": basic.get("online_status", False),
        "permission": "stream_preview",
    }


@router.get("/{token}/timesnap")
async def get_public_timesnap(token: str, db: Session = Depends(get_db)):
    """取得縮時排程資訊（供預覽用）"""
    inv, cam_token = await _get_public_cam(token, db)

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{CAMERA_BACKEND_URL}/api/cameras/{inv.camera_id}/timesnap",
            headers={"Authorization": f"Bearer {cam_token}"},
        )
    if resp.status_code != 200:
        return {"enable": False}
    return resp.json()


@router.get("/{token}/image")
async def get_public_nas_image(token: str, request: Request, db: Session = Depends(get_db)):
    """代理 NAS 圖片（供公開縮時預覽用）"""
    inv, cam_token = await _get_public_cam(token, db)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{CAMERA_BACKEND_URL}/api/camera/nas/image",
            headers={"Authorization": f"Bearer {cam_token}"},
            params=dict(request.query_params),
        )
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, "圖片無法取得")

    return StreamingResponse(
        iter([resp.content]),
        media_type=resp.headers.get("content-type", "image/jpeg"),
    )

import secrets as _secrets
import asyncio as _asyncio
from datetime import datetime as _dt

# In-memory 臨時圖片快取（60 秒有效）
_temp_image_cache: dict[str, tuple[bytes, str, float]] = {}  # token → (data, content_type, expires_at)

async def _store_temp_image(data: bytes, content_type: str) -> str:
    """存入快取，回傳 60 秒有效 token"""
    token = _secrets.token_urlsafe(16)
    expires = _asyncio.get_event_loop().time() + 60
    _temp_image_cache[token] = (data, content_type, expires)
    # 清理過期
    now = _asyncio.get_event_loop().time()
    expired = [k for k, (_, _, exp) in _temp_image_cache.items() if exp < now]
    for k in expired:
        del _temp_image_cache[k]
    return token


@router.get("/temp-image/{token}")
async def serve_temp_image(token: str):
    """公開端點：給 LINE 存取臨時圖片（60 秒有效）"""
    from fastapi.responses import Response
    entry = _temp_image_cache.get(token)
    if not entry:
        raise HTTPException(404, "圖片已過期或不存在")
    data, content_type, expires = entry
    import asyncio
    if asyncio.get_event_loop().time() > expires:
        del _temp_image_cache[token]
        raise HTTPException(410, "圖片已過期")
    return Response(content=data, media_type=content_type)


@router.get("/live-frame/{camera_id}")
async def live_camera_frame(camera_id: int, db: Session = Depends(get_db)):
    """直接從 go2rtc 取即時截圖，供 LINE 使用（公開，無需登入）"""
    from fastapi.responses import Response
    # 查 camera ip 推導 stream name
    import httpx as _httpx
    from routers.cameras import get_camera_backend_token, CAMERA_BACKEND_URL, CAMERA_SERVICE_KEY
    from models import User
    
    # 用 admin token 查 camera ip
    async with _httpx.AsyncClient(timeout=8) as cl:
        tok_r = await cl.post(f"{CAMERA_BACKEND_URL}/internal/auth/token",
            headers={"x-service-key": CAMERA_SERVICE_KEY},
            json={"user_id": 0, "email": "admin@timelapse.com", "role": "symotus_admin"})
    admin_tok = tok_r.json().get("access_token","") if tok_r.status_code == 200 else ""
    
    stream_name = None
    if admin_tok:
        async with _httpx.AsyncClient(timeout=8) as cl:
            cr = await cl.get(f"{CAMERA_BACKEND_URL}/api/cameras/{camera_id}",
                headers={"Authorization": f"Bearer {admin_tok}"})
        if cr.status_code == 200:
            info = cr.json(); basic = info.get("basic_info", info)
            ip = basic.get("ip_address", "")
            if ip and len(ip.split(".")) >= 3:
                stream_name = f"cam{ip.split('.')[2]}"
    
    if not stream_name:
        raise HTTPException(404, "找不到串流")
    
    # 從 go2rtc 取即時截圖
    async with _httpx.AsyncClient(timeout=15) as cl:
        gr = await cl.get(f"https://user.symotus.com/go2rtc/api/frame.jpeg?src={stream_name}")
    
    if gr.status_code != 200 or not gr.content:
        raise HTTPException(503, "相機目前無串流")
    
    return Response(
        content=gr.content,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-cache", "Content-Length": str(len(gr.content))}
    )
