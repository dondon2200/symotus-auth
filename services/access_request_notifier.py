"""相機權限請求的 LINE 通知（2026-11-23）。

- 有人送出請求 → 推給可核准的人（相機的全功能管理者＋symotus_admin），按鈕連到分享管理頁。
- 核准或拒絕 → 推給請求者；核准時按鈕直接連到該相機。

只推給有 is_active LINE 綁定的帳號；沒綁的人在分享管理頁看得到待處理清單。
推播是 fire-and-forget：失敗只記 log，不影響請求本身。
"""
import asyncio
import logging
import os

import httpx
from sqlalchemy.orm import Session

from services.job_notifier import owner_line_ids

logger = logging.getLogger(__name__)

LINE_ACCESS_TOKEN = os.getenv("LINE_ACCESS_TOKEN", "")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://user.symotus.com")

# 持有強引用，否則 event loop 只留弱引用，推播可能在送出前被 GC 掉
_pending: set[asyncio.Task] = set()


def build_push_payload(line_user_id: str, header: str, title: str, body_text: str,
                       button_label: str, url: str, color: str = "#f97316") -> dict:
    """組 LINE Flex 訊息；獨立成函式方便測試，不打網路。"""
    return {
        "to": line_user_id,
        "messages": [{
            "type": "flex",
            "altText": f"{header}：{title}",
            "contents": {
                "type": "bubble", "size": "kilo",
                "header": {
                    "type": "box", "layout": "vertical", "paddingAll": "16px",
                    "backgroundColor": color,
                    "contents": [{"type": "text", "text": header,
                                  "color": "#ffffff", "size": "sm", "weight": "bold"}],
                },
                "body": {
                    "type": "box", "layout": "vertical", "paddingAll": "16px", "spacing": "sm",
                    "contents": [
                        {"type": "text", "text": title, "weight": "bold", "size": "xl", "wrap": True},
                        {"type": "text", "text": body_text, "size": "sm", "color": "#666666", "wrap": True},
                    ],
                },
                "footer": {
                    "type": "box", "layout": "vertical", "paddingAll": "12px",
                    "contents": [{
                        "type": "button", "style": "primary", "color": color,
                        "action": {"type": "uri", "label": button_label, "uri": url},
                    }],
                },
            },
        }],
    }


async def _send(payload: dict) -> bool:
    if not LINE_ACCESS_TOKEN:
        logger.info("access_request_notifier: 未設定 LINE_ACCESS_TOKEN，略過推播")
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post("https://api.line.me/v2/bot/message/push",
                             headers={"Authorization": f"Bearer {LINE_ACCESS_TOKEN}",
                                      "Content-Type": "application/json"},
                             json=payload)
        if r.is_success:
            return True
        logger.warning("access_request_notifier: LINE push fail %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        logger.warning("access_request_notifier: LINE push error: %s", e)
    return False


def _schedule(payloads: list[dict]) -> int:
    """排入推播，回傳排了幾則（測試用）。沒有 running loop（例如同步測試）就只記錄不推。"""
    if not payloads:
        return 0
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.info("access_request_notifier: 沒有 event loop，略過 %d 則推播", len(payloads))
        return 0
    for p in payloads:
        task = loop.create_task(_send(p))
        _pending.add(task)
        task.add_done_callback(_pending.discard)
    return len(payloads)


def new_request_payloads(db: Session, approver_user_ids: list[int], requester_name: str,
                         camera_name: str) -> list[dict]:
    url = f"{FRONTEND_URL}/shares?tab=requests"
    out = []
    for uid in approver_user_ids:
        for lid in owner_line_ids(db, uid):
            out.append(build_push_payload(
                lid, "🔑 相機權限請求", camera_name,
                f"{requester_name} 想要存取這台相機，請到分享管理核准或拒絕。",
                "處理請求", url))
    return out


def decision_payloads(db: Session, requester_id: int, camera_name: str, approved: bool,
                      camera_id: int) -> list[dict]:
    if approved:
        header, body, label, color = "✅ 權限請求已核准", "你現在可以查看這台相機了。", "開啟相機", "#f97316"
        url = f"{FRONTEND_URL}/camera/{camera_id}"
    else:
        header, body, label, color = "權限請求未通過", "相機管理者沒有核准這次請求，如有疑問請直接聯絡對方。", "回到儀表板", "#6b7280"
        url = f"{FRONTEND_URL}/dashboard"
    return [build_push_payload(lid, header, camera_name, body, label, url, color)
            for lid in owner_line_ids(db, requester_id)]


def notify_new_request(db: Session, approver_user_ids: list[int], requester_name: str,
                       camera_name: str) -> int:
    return _schedule(new_request_payloads(db, approver_user_ids, requester_name, camera_name))


def notify_decision(db: Session, requester_id: int, camera_name: str, approved: bool,
                    camera_id: int) -> int:
    return _schedule(decision_payloads(db, requester_id, camera_name, approved, camera_id))
