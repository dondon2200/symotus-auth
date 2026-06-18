"""
背景工作：每 60 秒檢查相機狀態，每次「開機上線」推 LINE 通知。

相機多為排程式定時開關機（例：08:00-20:00 每 15 分鐘開機一次、每次約數分鐘），
因此每一次真正的開機都應通知。為避免單次開機 session 內因心跳抖動造成的
瞬間 online→offline→online 重複通知，採「離線確認」機制：相機要連續離線達
OFFLINE_CONFIRM_POLLS 次輪詢才視為「真的關機」，才為下一次開機重新武裝。
"""
import asyncio, logging, httpx, os
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy.orm import Session
from database import SessionLocal
from models import User, CameraAccess

logger = logging.getLogger(__name__)

TW_TZ = ZoneInfo("Asia/Taipei")   # 容器跑 UTC，顯示時間一律轉台北時間

CAMERA_BACKEND_URL = "https://user.symotus.com"
CAMERA_SERVICE_KEY = os.getenv("CAMERA_SERVICE_KEY", "")
LINE_ACCESS_TOKEN  = os.getenv("LINE_ACCESS_TOKEN", "")
FRONTEND_URL       = os.getenv("FRONTEND_URL", "https://user.symotus.com")
CHECK_INTERVAL        = 60   # 每 60 秒輪詢一次
OFFLINE_CONFIRM_POLLS = 3    # 連續離線 N 次輪詢(≈3 分鐘)才視為「真的關機」，可再為下次開機通知

# 每台相機是否已為「本次開機」發過通知（True=已發、不再重發；離線確認後重置為 False）
_notified: dict[int, bool] = {}
# 連續離線輪詢次數；達 OFFLINE_CONFIRM_POLLS 才確認關機並重新武裝
_offline_streak: dict[int, int] = {}


async def _get_admin_token() -> str:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{CAMERA_BACKEND_URL}/internal/auth/token",
            headers={"x-service-key": CAMERA_SERVICE_KEY},
            json={"user_id": 0, "email": "admin@timelapse.com", "role": "admin"})
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
                        {"type": "text", "text": datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M"),
                         "size": "xs", "color": "#aaaaaa"}
                    ]
                },
                "footer": {
                    "type": "box", "layout": "vertical", "paddingAll": "12px", "spacing": "sm",
                    "contents": [
                        {
                            "type": "button", "style": "primary", "color": "#f97316",
                            "action": {"type": "uri", "label": "查看相機", "uri": url}
                        },
                        {
                            "type": "button", "style": "secondary",
                            "action": {
                                "type": "message",
                                "label": "取消開機通知",
                                "text": f"取消相機通知 {camera_id}"
                            }
                        }
                    ]
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
    # camera_access 裡有 line_id 且 notify_on_online=True 的用戶
    for acc in db.query(CameraAccess).filter(CameraAccess.camera_id == camera_id).all():
        # 只通知有訂閱的用戶（notify_on_online 預設 True）
        notify = getattr(acc, "notify_on_online", True)
        if not notify:
            continue
        u = db.query(User).filter(User.id == acc.user_id, User.line_id.isnot(None)).first()
        if u:
            ids.add(u.line_id)
    return list(ids)


async def check_and_notify():
    cameras = await get_camera_list()
    if not cameras:
        return
    db = SessionLocal()
    try:
        for cam in cameras:
            cam_id = cam.get("id")
            is_online = bool(cam.get("online_status", False))   # 正規化：避免 0/None 與 False 比較落差
            if is_online:
                if not _notified.get(cam_id, False):
                    # 本次開機尚未通知過 → 推播一次
                    cam_name = cam.get("name", f"相機 {cam_id}")
                    logger.info(f"{cam_name} booted online → pushing LINE")
                    for lid in get_notify_line_ids(cam_id, db):
                        await send_line_push(lid, cam_id, cam_name)
                    _notified[cam_id] = True
                _offline_streak[cam_id] = 0
            else:
                # 連續離線達門檻才確認「真的關機」，為下次開機重新武裝
                _offline_streak[cam_id] = _offline_streak.get(cam_id, 0) + 1
                if _offline_streak[cam_id] >= OFFLINE_CONFIRM_POLLS:
                    _notified[cam_id] = False
    finally:
        db.close()


async def start_camera_notifier():
    """初始化狀態快取，然後每 60 秒檢查一次"""
    logger.info("Camera notifier starting...")
    cameras = await get_camera_list()
    for cam in cameras:
        cid = cam.get("id")
        online = bool(cam.get("online_status", False))
        _notified[cid] = online                                   # 啟動時已在線者不補通知
        _offline_streak[cid] = 0 if online else OFFLINE_CONFIRM_POLLS  # 已離線者立即武裝
    logger.info(f"Watching {len(_notified)} cameras")
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        try:
            await check_and_notify()
        except Exception as e:
            logger.warning(f"notifier loop error: {e}")
