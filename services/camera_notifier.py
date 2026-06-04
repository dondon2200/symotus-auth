"""
背景工作：每 5 分鐘檢查相機狀態，開機時推 LINE 通知
"""
import asyncio, logging, httpx
from datetime import datetime
from sqlalchemy.orm import Session
from database import SessionLocal
from models import User, CameraAccess

logger = logging.getLogger(__name__)

CAMERA_BACKEND_URL = "https://user.symotus.com"
CAMERA_SERVICE_KEY = "9ad3343a32508c209152a450f601b990176fa4d41c94c27330e448b1a86826c2"
LINE_ACCESS_TOKEN  = "ShTSgT5SabcpwdKVqTQUR5toy4/UdRWr+oxQpQYYKeswYmwD1mJ3NuD0velI+mXPDNSX5VJiTfWjF60Ji7scmd1Mawyn2jCGPg6LmOuRSbs7UQKr/tN8QaMnb028Zuazo/WMSmuDzZGkX/agdTKymAdB04t89/1O/w1cDnyilFU="
FRONTEND_URL       = "https://admin.symotus.com"
CHECK_INTERVAL     = 60   # 1 分鐘

_prev_status: dict[int, bool] = {}


async def _get_admin_token() -> str:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{CAMERA_BACKEND_URL}/internal/auth/token",
            headers={"x-service-key": CAMERA_SERVICE_KEY},
            json={"user_id": 0, "email": "admin@timelapse.com", "role": "symotus_admin"})
        return r.json().get("access_token", "") if r.is_success else ""


async def get_camera_list() -> list[dict]:
    try:
        token = await _get_admin_token()
        if not token:
            return []
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{CAMERA_BACKEND_URL}/api/cameras",
                headers={"Authorization": f"Bearer {token}"})
            return r.json().get("cameras", []) if r.is_success else []
    except Exception as e:
        logger.warning(f"camera_notifier: {e}")
        return []


async def send_line_push(line_user_id: str, camera_id: int, camera_name: str):
    url = f"{FRONTEND_URL}/camera/{camera_id}"
    payload = {
        "to": line_user_id,
        "messages": [{
            "type": "flex",
            "altText": f"📷 {camera_name} 已開機上線",
            "contents": {
                "type": "bubble", "size": "kilo",
                "header": {
                    "type": "box", "layout": "vertical", "paddingAll": "16px",
                    "backgroundColor": "#f97316",
                    "contents": [{"type": "text", "text": "📷 相機開機通知",
                                  "color": "#ffffff", "size": "sm", "weight": "bold"}]
                },
                "body": {
                    "type": "box", "layout": "vertical", "paddingAll": "16px", "spacing": "sm",
                    "contents": [
                        {"type": "text", "text": camera_name, "weight": "bold", "size": "xl"},
                        {"type": "text", "text": "已成功開機上線，可開始拍照",
                         "size": "sm", "color": "#666666", "wrap": True},
                        {"type": "text", "text": datetime.now().strftime("%Y-%m-%d %H:%M"),
                         "size": "xs", "color": "#aaaaaa"}
                    ]
                },
                "footer": {
                    "type": "box", "layout": "vertical", "paddingAll": "12px",
                    "contents": [{
                        "type": "button", "style": "primary", "color": "#f97316",
                        "action": {"type": "uri", "label": "查看相機", "uri": url}
                    }]
                }
            }
        }]
    }
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post("https://api.line.me/v2/bot/message/push",
                headers={"Authorization": f"Bearer {LINE_ACCESS_TOKEN}",
                         "Content-Type": "application/json"},
                json=payload)
            if r.is_success:
                logger.info(f"LINE push OK → {line_user_id} ({camera_name})")
            else:
                logger.warning(f"LINE push fail: {r.status_code}")
    except Exception as e:
        logger.warning(f"LINE push error: {e}")


def get_notify_line_ids(camera_id: int, db: Session) -> list[str]:
    """找應收通知的所有 LINE user ID"""
    ids = set()
    # admin 一定通知
    for u in db.query(User).filter(User.role == "symotus_admin", User.line_id.isnot(None)).all():
        ids.add(u.line_id)
    # camera_access 裡有 line_id 的用戶 & 授權者
    for acc in db.query(CameraAccess).filter(CameraAccess.camera_id == camera_id).all():
        for uid in [acc.user_id, acc.granted_by]:
            if uid:
                u = db.query(User).filter(User.id == uid, User.line_id.isnot(None)).first()
                if u:
                    ids.add(u.line_id)
    return list(ids)


async def check_and_notify():
    global _prev_status
    cameras = await get_camera_list()
    if not cameras:
        return
    db = SessionLocal()
    try:
        for cam in cameras:
            cam_id = cam.get("id")
            is_online = cam.get("online_status", False)
            if _prev_status.get(cam_id) is False and is_online:
                cam_name = cam.get("name", f"相機 {cam_id}")
                logger.info(f"{cam_name} came online → pushing LINE")
                for lid in get_notify_line_ids(cam_id, db):
                    await send_line_push(lid, cam_id, cam_name)
            _prev_status[cam_id] = is_online
    finally:
        db.close()


async def start_camera_notifier():
    """初始化狀態快取，然後每 5 分鐘檢查一次"""
    logger.info("Camera notifier starting...")
    cameras = await get_camera_list()
    for cam in cameras:
        _prev_status[cam.get("id")] = cam.get("online_status", False)
    logger.info(f"Watching {len(_prev_status)} cameras")
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        try:
            await check_and_notify()
        except Exception as e:
            logger.warning(f"notifier loop error: {e}")
