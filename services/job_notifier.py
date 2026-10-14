"""縮時任務完成／失敗的 LINE 通知，與不靠前端的背景同步。

背景（2026-10-14）：任務頁原本有「完成時通知我」按鈕，打的是 `/cameras/notify-job-complete`，
但 auth 從來沒有這支端點——按了只會得到錯誤。而且任務狀態只在使用者開著任務頁時才會
與 Spark 對齊（GET /jobs 順手同步、GET /jobs/gdrive/{id} 輪詢），關掉分頁就沒人知道
它做完了。

這裡做兩件事：
1. `schedule_job_notification()`：任何地方把任務寫成終態（completed/failed）時呼叫，
   推 LINE 給任務的建立者（所有 is_active 的 LINE 綁定）。同一任務 10 分鐘內只推一次，
   擋住兩個並發的列表請求同時看到轉態的情況。
2. `start_job_sync_loop()`：每 JOB_SYNC_INTERVAL 秒把未結束的相機任務與 GDrive 任務
   向 Spark 對齊一次，使用者不開頁面也會轉態並收到通知。
"""
import asyncio
import logging
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.orm import Session

from database import SessionLocal
from models import User, UserLineAccount

logger = logging.getLogger(__name__)

TW_TZ = ZoneInfo("Asia/Taipei")
LINE_ACCESS_TOKEN = os.getenv("LINE_ACCESS_TOKEN", "")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://user.symotus.com")
JOB_SYNC_INTERVAL = int(os.getenv("JOB_SYNC_INTERVAL", "120"))
# 同一任務的重複通知抑制窗口（秒）
DEDUP_WINDOW_SECS = 600
# 轉態時 Spark 的完成時間若早於這個時數，視為「服務停擺期間早就完成的舊任務」，
# 只對齊狀態不推播，避免重啟後一口氣補發一堆陳年通知。
STALE_HOURS = 24

_recent: dict[str, float] = {}
# 持有 fire-and-forget 任務的強引用，否則 event loop 只留弱引用，推播可能在送出前被 GC 掉
_pending: set[asyncio.Task] = set()


def _job_url(kind: str, key) -> str:
    prefix = "cam" if kind == "camera" else "gd"
    return f"{FRONTEND_URL}/jobs?job={prefix}:{key}"


def build_job_push_payload(line_user_id: str, title: str, status: str, url: str) -> dict:
    """組 LINE Flex 訊息；獨立成函式方便測試，不打網路。"""
    ok = status == "completed"
    header = "🎬 縮時影片完成" if ok else "⚠️ 縮時影片失敗"
    body_text = "影片已經做好，可以觀看或下載了" if ok else "處理時發生問題，請到任務頁查看原因"
    color = "#f97316" if ok else "#b91c1c"
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
                        {"type": "text", "text": title or "縮時任務", "weight": "bold", "size": "xl", "wrap": True},
                        {"type": "text", "text": body_text, "size": "sm", "color": "#666666", "wrap": True},
                        {"type": "text", "text": datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M"),
                         "size": "xs", "color": "#aaaaaa"},
                    ],
                },
                "footer": {
                    "type": "box", "layout": "vertical", "paddingAll": "12px",
                    "contents": [{
                        "type": "button", "style": "primary", "color": color,
                        "action": {"type": "uri", "label": "查看任務", "uri": url},
                    }],
                },
            },
        }],
    }


async def send_job_push(line_user_id: str, title: str, status: str, url: str) -> bool:
    if not LINE_ACCESS_TOKEN:
        logger.info("job_notifier: 未設定 LINE_ACCESS_TOKEN，略過推播")
        return False
    payload = build_job_push_payload(line_user_id, title, status, url)
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post("https://api.line.me/v2/bot/message/push",
                             headers={"Authorization": f"Bearer {LINE_ACCESS_TOKEN}",
                                      "Content-Type": "application/json"},
                             json=payload)
        if r.is_success:
            logger.info("job_notifier: LINE push OK → %s (%s %s)", line_user_id, status, title)
            return True
        logger.warning("job_notifier: LINE push fail %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        logger.warning("job_notifier: LINE push error: %s", e)
    return False


def owner_line_ids(db: Session, user_id: int) -> list[str]:
    """任務建立者目前有效（is_active）的 LINE user id。"""
    rows = db.query(UserLineAccount).filter(
        UserLineAccount.user_id == user_id,
        UserLineAccount.is_active == True,  # noqa: E712
    ).all()
    return [r.line_user_id for r in rows]


def _is_stale(completed_at) -> bool:
    if not completed_at:
        return False
    try:
        return completed_at < datetime.utcnow() - timedelta(hours=STALE_HOURS)
    except TypeError:
        return False


def schedule_job_notification(db: Session, kind: str, key, owner_id: int, title: str,
                              status: str, completed_at=None) -> bool:
    """在任務轉成終態後呼叫。回傳是否真的排了推播（測試用）。

    kind: "camera" | "gdrive"；key: 相機任務用 Spark job_id、GDrive 用本地 id。
    要在 running loop 裡呼叫（FastAPI handler 或背景工作皆可）；沒有 loop 就只記錄不推。
    """
    if status not in ("completed", "failed"):
        return False
    dedup_key = f"{kind}:{key}"
    now = time.monotonic()
    for k, t in list(_recent.items()):
        if now - t > DEDUP_WINDOW_SECS:
            _recent.pop(k, None)
    if dedup_key in _recent:
        return False
    if status == "completed" and _is_stale(completed_at):
        logger.info("job_notifier: %s 完成時間過舊，只對齊狀態不推播", dedup_key)
        _recent[dedup_key] = now
        return False
    line_ids = owner_line_ids(db, owner_id)
    _recent[dedup_key] = now
    if not line_ids:
        return False
    url = _job_url(kind, key)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("job_notifier: 沒有 event loop，略過 %s 的推播", dedup_key)
        return False
    for lid in line_ids:
        task = loop.create_task(send_job_push(lid, title, status, url))
        _pending.add(task)
        task.add_done_callback(_pending.discard)
    return True


async def sync_once() -> None:
    """把未結束的任務向 Spark 對齊一次（相機任務＋已送 Spark 的 GDrive 任務）。"""
    # 延遲 import：routers.jobs 在模組頂層 import 本模組，這裡反向 import 要等到執行期
    from models import TimelapsJob, GDriveJob
    from routers import jobs as jobs_module

    db = SessionLocal()
    try:
        cam_jobs = db.query(TimelapsJob).filter(
            ~TimelapsJob.status.in_(("completed", "failed", "Completed", "Failed"))
        ).all()
        if cam_jobs:
            await jobs_module._sync_jobs_with_spark(db, cam_jobs)
        gd_jobs = db.query(GDriveJob).filter(
            GDriveJob.status == "processing",
            GDriveJob.spark_job_id.isnot(None),
        ).all()
        for job in gd_jobs:
            await jobs_module.sync_gdrive_job_with_spark(db, job)
    finally:
        db.close()


async def start_job_sync_loop() -> None:
    logger.info("job sync loop starting (every %ss)", JOB_SYNC_INTERVAL)
    while True:
        await asyncio.sleep(JOB_SYNC_INTERVAL)
        try:
            await sync_once()
        except Exception as e:
            logger.warning("job sync loop error: %s", e)
