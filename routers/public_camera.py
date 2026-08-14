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

import hmac as _hmac, hashlib as _hashlib, time as _time
from config import settings

router = APIRouter(prefix="/cameras/public", tags=["public-camera"])

GO2RTC_BASE = "https://user.symotus.com/go2rtc"

# ── go2rtc stream 名稱解析 ────────────────────────────────────────
# go2rtc 的 stream 名稱不是固定 cam<IP第三段>：LAN 相機（192.168.N.1）才命名成 cam<N>；
# 公網 IP 相機是流水號（cam1=110.25.96.4、cam2=110.25.96.15…）跟 IP 無關。
# 硬推 cam<第三段> 會對公網 IP 相機算出不存在的串流（110.25.96.15 → cam96 → 404），
# 分享連結因此永遠播不出畫面。改由 go2rtc 設定的 source host 反查，與前端 pickStreamName 同一套規則。
import re as _re

_STREAM_RE = _re.compile(r"^\s*(cam\w+):\s*rtsp://([0-9.]+)", _re.MULTILINE)
_stream_map_cache: tuple[list[tuple[str, str]], float] | None = None  # ([(name, host)], expires_at)
_STREAM_MAP_TTL = 60.0


async def _get_stream_map() -> list[tuple[str, str]]:
    """抓 go2rtc /api/config 解析出 [(stream 名稱, source host)]，快取 60 秒。抓不到回空 list。"""
    global _stream_map_cache
    now = _time.time()
    if _stream_map_cache and _stream_map_cache[1] > now:
        return _stream_map_cache[0]
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(f"{GO2RTC_BASE}/api/config")
        pairs = _STREAM_RE.findall(resp.text) if resp.status_code == 200 else []
    except Exception:
        pairs = []
    if pairs:  # 抓失敗不覆蓋既有快取，免得一次網路抖動就讓所有相機退回硬推
        _stream_map_cache = (pairs, now + _STREAM_MAP_TTL)
    return pairs or (_stream_map_cache[0] if _stream_map_cache else [])


def _pick_stream_name(ip: str, pairs: list[tuple[str, str]]) -> str:
    """由相機 IP 對應 go2rtc stream 名稱：
    1) source host 完全相符（含公網 IP 相機，如 110.25.96.15 → cam2）
    2) 同 /24 子網且唯一相符（LAN 相機 device .100 對到 NVR .1 的 cam<N>）
    3) 退路：cam<IP第三段>（go2rtc 設定抓不到時的舊慣例）
    """
    if not ip:
        return ""
    for name, host in pairs:
        if host == ip:
            return name
    sub24 = lambda x: ".".join(x.split(".")[:3])
    matches = [name for name, host in pairs if sub24(host) == sub24(ip)]
    if len(matches) == 1:
        return matches[0]
    octets = ip.split(".")
    return f"cam{octets[2]}" if len(octets) >= 3 else ""


async def resolve_stream_name(ip: str) -> str:
    return _pick_stream_name(ip, await _get_stream_map())


async def _stream_is_live(stream_name: str) -> bool:
    """go2rtc 是 on-demand 拉流，producers/bytes_recv 不可靠；改抓一張 frame.jpeg，
    有效幀為數十 KB，裝置關機則約 5 秒後回 200 空 body。"""
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            resp = await client.get(f"{GO2RTC_BASE}/api/frame.jpeg", params={"src": stream_name})
        return resp.status_code == 200 and len(resp.content) > 1024
    except Exception:
        return False


def _live_frame_sig(camera_id: int, exp: int) -> str:
    """F-4：live-frame 簽章（HMAC-SHA256，防以 camera_id 枚舉任意相機畫面）"""
    msg = f"{camera_id}:{exp}".encode()
    return _hmac.new(settings.JWT_SECRET.encode(), msg, _hashlib.sha256).hexdigest()


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
    """取得公開分享相機資訊。D6：公開頁不含即時串流，僅縮時預覽＋相簿檢視，
    故不再回傳 stream_name/online（也省去每次開頁打 go2rtc 探活）。"""
    inv, _cam_token = await _get_public_cam(token, db)
    return {
        "camera_id": inv.camera_id,
        "camera_name": inv.camera_name or f"相機 #{inv.camera_id}",
        "permission": "preview",
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


@router.get("/{token}/images")
async def get_public_nas_images(token: str, request: Request, db: Session = Depends(get_db)):
    """公開相簿/縮時預覽列表：走與登入版相同的列表核心（serial 解析＋日期資料夾掃描）。
    camera_id 一律強制取自邀請（client 不可指定其他相機）；僅放行分頁與日期區間參數。
    緣由：nas/image（單數）不支援目錄列表，舊版以 timesnap.serial 拼路徑的做法從未生效。"""
    from routers.cameras import list_nas_images_backend
    inv, cam_token = await _get_public_cam(token, db)
    params = {"camera_id": str(inv.camera_id)}
    for k in ("limit", "offset", "start_time", "end_time"):
        v = request.query_params.get(k)
        if v is not None:
            params[k] = v
    return await list_nas_images_backend(cam_token, str(inv.camera_id), params)


# 相機 serial 快取（camera_id → (serial, expires_at)）：/image 每張都要驗路徑歸屬，
# 不快取就等於每張圖多一次 Camera Backend 查詢（見 2026-08-14 連線池耗盡事故）。
_SERIAL_TTL = 300.0
_serial_cache: dict[int, tuple[str, float]] = {}


async def _camera_serial(camera_id: int, cam_token: str) -> str:
    now = _time.monotonic()
    hit = _serial_cache.get(camera_id)
    if hit and hit[1] > now:
        return hit[0]
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{CAMERA_BACKEND_URL}/api/cameras/{camera_id}",
            headers={"Authorization": f"Bearer {cam_token}"},
        )
    if resp.status_code != 200:
        return ""
    basic = resp.json().get("basic_info", resp.json())
    serial = basic.get("device_serial_id") or basic.get("serial_id") or basic.get("serial") or ""
    if serial:
        _serial_cache[camera_id] = (serial, now + _SERIAL_TTL)
    return serial


@router.get("/{token}/image")
async def get_public_nas_image(token: str, request: Request, db: Session = Depends(get_db)):
    """代理 NAS 圖片（公開縮時預覽與相簿放大檢視用）。
    只轉發白名單參數（path/thumbnail/limit），其餘來路參數一律丟棄。

    path 必須落在本邀請相機自己的 serial 目錄下（2026-08-14 補洞：分享者名下有多台
    相機時，同一顆 granter token 對所有相機都有效，未驗歸屬等於任一公開連結可讀取
    該帳號其他相機的照片）。列表端點本就只回本機 serial 的路徑，故不影響正常使用。

    thumbnail 由呼叫端決定（預設 true）：2026-08-14 產品決議——公開頁的相簿放大檢視
    顯示原圖。「不可下載」自此僅指不提供下載按鈕與批次取檔入口（UI 層），
    非伺服器層阻擋，spec 誠實邊界已同步更新。"""
    inv, cam_token = await _get_public_cam(token, db)

    path = request.query_params.get("path")
    if not path:
        raise HTTPException(400, "缺少 path 參數")

    serial = await _camera_serial(inv.camera_id, cam_token)
    if not serial or not path.startswith(f"/homes/firmness/{serial}/"):
        raise HTTPException(403, "此連結無權存取該路徑")

    thumb = request.query_params.get("thumbnail")
    params = {"path": path, "thumbnail": "true" if thumb is None else thumb}
    limit = request.query_params.get("limit")
    if limit is not None:
        params["limit"] = limit

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{CAMERA_BACKEND_URL}/api/camera/nas/image",
            headers={"Authorization": f"Bearer {cam_token}"},
            params=params,
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
async def live_camera_frame(camera_id: int, exp: int = 0, sig: str = "", db: Session = Depends(get_db)):
    """直接從 go2rtc 取即時截圖，供 LINE 使用（公開但需簽章）。
    F-4：必須帶有效 exp+sig（由 line_webhook 預簽），否則 403，杜絕以 camera_id 枚舉任意相機畫面。"""
    from fastapi.responses import Response
    if not sig or exp < int(_time.time()) or not _hmac.compare_digest(sig, _live_frame_sig(camera_id, exp)):
        raise HTTPException(403, "連結無效或已過期")
    # 查 camera ip 推導 stream name
    import httpx as _httpx
    from routers.cameras import get_camera_backend_token, CAMERA_BACKEND_URL, CAMERA_SERVICE_KEY
    from models import User
    
    # 用 admin token 查 camera ip
    async with _httpx.AsyncClient(timeout=8) as cl:
        tok_r = await cl.post(f"{CAMERA_BACKEND_URL}/internal/auth/token",
            headers={"x-service-key": CAMERA_SERVICE_KEY},
            json={"user_id": 0, "email": "admin@timelapse.com", "role": "admin"})
    admin_tok = tok_r.json().get("access_token","") if tok_r.status_code == 200 else ""
    
    stream_name = None
    if admin_tok:
        async with _httpx.AsyncClient(timeout=8) as cl:
            cr = await cl.get(f"{CAMERA_BACKEND_URL}/api/cameras/{camera_id}",
                headers={"Authorization": f"Bearer {admin_tok}"})
        if cr.status_code == 200:
            info = cr.json(); basic = info.get("basic_info", info)
            ip = basic.get("ip_address", "")
            # 同上：公網 IP 相機不能硬推 cam<第三段>，否則 LINE 即時截圖也會抓到不存在的串流
            stream_name = await resolve_stream_name(ip) or None
    
    if not stream_name:
        raise HTTPException(404, "找不到串流")
    
    # 從 go2rtc 取即時截圖
    async with _httpx.AsyncClient(timeout=15) as cl:
        gr = await cl.get(f"{GO2RTC_BASE}/api/frame.jpeg", params={"src": stream_name})
    
    if gr.status_code != 200 or not gr.content:
        raise HTTPException(503, "相機目前無串流")
    
    return Response(
        content=gr.content,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-cache", "Content-Length": str(len(gr.content))}
    )
