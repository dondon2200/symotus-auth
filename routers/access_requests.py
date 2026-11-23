"""
相機存取權限請求（2026-11-23）

新增相機時，掃到或輸入的相機若系統裡已經有：
- 自己有權限 → 前端直接進相機設定頁（POST /access-requests/lookup 回 has_access=True）。
- 自己沒權限 → 送出權限請求（POST /access-requests），由該相機的全功能管理者
  或 symotus_admin 在分享管理頁核准（選權限等級）或拒絕；兩邊都會收到 LINE 通知。

為什麼不放在 /cameras 底下：/cameras/{camera_id}/{path} 是通用代理 catch-all，
而且這裡的端點都不以路徑帶 camera_id（相機還不屬於請求者，鎖機閘門不適用於「查詢」）。
"""
import logging
from datetime import datetime
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from audit import log_action
from auth import get_current_user
from database import get_db
from models import CameraAccess, CameraAccessRequest, User
from routers import cameras as cameras_mod
from routers.auth import _rate_limit
from schemas import utc_iso
from services.access_request_notifier import notify_decision, notify_new_request
from services.billing_v2 import assert_camera_unlocked

router = APIRouter(prefix="/access-requests", tags=["access-requests"])
logger = logging.getLogger(__name__)

PERMISSION_LEVELS = ("full", "photos_stream", "stream_only")


def _display_name(u: Optional[User]) -> str:
    if not u:
        return "未知"
    return u.full_name or u.username or u.email or f"使用者 #{u.id}"


async def _all_cameras() -> list[dict]:
    """用 admin token 取 Camera Backend 的完整相機清單。

    查「這台相機系統裡有沒有」不能用請求者自己的 token —— 他看不到別人的相機，
    這正是要查的情況。取不到清單時拋 502，不要回「不存在」：那會讓前端接著建立重複的相機。"""
    tok = await cameras_mod._get_admin_camera_token()
    if not tok:
        raise HTTPException(502, detail={"code": "CAMERA_BACKEND_UNAVAILABLE", "message": "暫時查不到相機資料，請稍後再試"})
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{cameras_mod.CAMERA_BACKEND_URL}/api/cameras",
                headers={"Authorization": f"Bearer {tok}"},
                params={"limit": 1000},
            )
    except Exception as e:
        logger.warning("access_requests: 取相機清單失敗: %s", e)
        raise HTTPException(502, detail={"code": "CAMERA_BACKEND_UNAVAILABLE", "message": "暫時查不到相機資料，請稍後再試"})
    if resp.status_code != 200:
        logger.warning("access_requests: 取相機清單回 %s", resp.status_code)
        raise HTTPException(502, detail={"code": "CAMERA_BACKEND_UNAVAILABLE", "message": "暫時查不到相機資料，請稍後再試"})
    return resp.json().get("cameras", []) or []


def find_camera(cameras: list[dict], ip_address: Optional[str], port: Optional[int],
                serial: Optional[str]) -> Optional[dict]:
    """先比序號（機身唯一），比不到再比 IP＋埠號。"""
    serial = (serial or "").strip()
    if serial:
        for c in cameras:
            if str(c.get("device_serial_id") or "").strip() == serial:
                return c
    ip = (ip_address or "").strip()
    if ip:
        for c in cameras:
            if c.get("ip_address") == ip and (port is None or c.get("port") == port):
                return c
    return None


def _has_access(db: Session, user: User, camera_id: int) -> bool:
    allowed = cameras_mod.get_allowed_camera_ids(user, db)
    return allowed is None or camera_id in allowed


def _pending(db: Session, user_id: int, camera_id: int) -> Optional[CameraAccessRequest]:
    return db.query(CameraAccessRequest).filter(
        CameraAccessRequest.requester_id == user_id,
        CameraAccessRequest.camera_id == camera_id,
        CameraAccessRequest.status == "pending",
    ).order_by(CameraAccessRequest.id.desc()).first()


def approver_ids(db: Session, camera_id: int) -> list[int]:
    """可核准這台相機請求的人：全功能管理者 ＋ 所有啟用中的 symotus_admin。"""
    ids = {a.user_id for a in db.query(CameraAccess).filter(
        CameraAccess.camera_id == camera_id,
        CameraAccess.permission_level == "full",
    ).all()}
    ids |= {u.id for u in db.query(User).filter(
        User.role == "symotus_admin", User.is_active == True,  # noqa: E712
    ).all()}
    return sorted(ids)


def _can_decide(db: Session, user: User, camera_id: int) -> bool:
    if user.role == "symotus_admin":
        return True
    return db.query(CameraAccess).filter(
        CameraAccess.camera_id == camera_id,
        CameraAccess.user_id == user.id,
        CameraAccess.permission_level == "full",
    ).first() is not None


# ── 查詢：這台相機存在嗎、我有沒有權限 ────────────────────────────────────────
class LookupBody(BaseModel):
    ip_address: Optional[str] = Field(default=None, max_length=64)
    port: Optional[int] = None
    serial: Optional[str] = Field(default=None, max_length=64)


@router.post("/lookup")
async def lookup_camera(
    body: LookupBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """新增相機前先查。沒權限的人只拿得到 camera_id 與自己的請求狀態，拿不到相機名稱
    —— IP 很好猜，不能讓人靠掃 IP 列出別人的工地名稱。"""
    _rate_limit(request, "camera_lookup", 30)
    if not (body.ip_address or "").strip() and not (body.serial or "").strip():
        raise HTTPException(400, "請提供相機的 IP 位址或序號")
    cam = find_camera(await _all_cameras(), body.ip_address, body.port, body.serial)
    if not cam or cam.get("id") is None:
        return {"exists": False}
    camera_id = int(cam["id"])
    if _has_access(db, current_user, camera_id):
        return {"exists": True, "camera_id": camera_id, "has_access": True,
                "camera_name": cam.get("name")}
    return {"exists": True, "camera_id": camera_id, "has_access": False,
            "request_pending": _pending(db, current_user.id, camera_id) is not None}


# ── 送出請求 ──────────────────────────────────────────────────────────────────
class CreateRequestBody(BaseModel):
    camera_id: int
    note: Optional[str] = Field(default=None, max_length=200)


@router.post("")
async def create_access_request(
    body: CreateRequestBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _rate_limit(request, "access_request_create", 10)
    if _has_access(db, current_user, body.camera_id):
        raise HTTPException(400, detail={"code": "ALREADY_HAS_ACCESS", "message": "您已經有這台相機的權限"})

    existing = _pending(db, current_user.id, body.camera_id)
    if existing:
        return {"id": existing.id, "status": "pending", "reused": True}

    cam = next((c for c in await _all_cameras() if c.get("id") == body.camera_id), None)
    if not cam:
        raise HTTPException(404, "相機不存在")
    # 計費 v2：鎖定中的相機不開放新的存取（與接受分享連結同一條規則）
    assert_camera_unlocked(db, current_user, body.camera_id)

    req = CameraAccessRequest(
        camera_id=body.camera_id,
        camera_name=cam.get("name") or f"相機 #{body.camera_id}",
        requester_id=current_user.id,
        note=(body.note or "").strip() or None,
        status="pending",
    )
    db.add(req)
    db.flush()
    log_action(db, current_user, "camera_access_request", "camera_access_request", req.id,
               f"camera_id={body.camera_id}")
    db.commit()

    notify_new_request(db, approver_ids(db, body.camera_id), _display_name(current_user), req.camera_name)
    return {"id": req.id, "status": "pending"}


# ── 待我處理的請求 ────────────────────────────────────────────────────────────
@router.get("/incoming")
def list_incoming(
    status: str = "pending",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """admin 看全部；其他人只看自己有全功能管理權限的相機。status=all 連已處理的一起回（最近 100 筆）。"""
    q = db.query(CameraAccessRequest)
    if status != "all":
        q = q.filter(CameraAccessRequest.status == status)
    if current_user.role != "symotus_admin":
        mine = [a.camera_id for a in db.query(CameraAccess).filter(
            CameraAccess.user_id == current_user.id,
            CameraAccess.permission_level == "full",
        ).all()]
        if not mine:
            return []
        q = q.filter(CameraAccessRequest.camera_id.in_(mine))
    rows = q.order_by(CameraAccessRequest.created_at.desc()).limit(100).all()
    users = {u.id: u for u in db.query(User).filter(
        User.id.in_({r.requester_id for r in rows} | {r.decided_by for r in rows if r.decided_by})
    ).all()} if rows else {}
    return [{
        "id": r.id,
        "camera_id": r.camera_id,
        "camera_name": r.camera_name or f"相機 #{r.camera_id}",
        "requester_name": _display_name(users.get(r.requester_id)),
        "requester_email": getattr(users.get(r.requester_id), "email", None),
        "note": r.note,
        "status": r.status,
        "permission_level": r.permission_level,
        "decided_by_name": _display_name(users.get(r.decided_by)) if r.decided_by else None,
        "created_at": utc_iso(r.created_at),
        "decided_at": utc_iso(r.decided_at),
    } for r in rows]


# ── 核准／拒絕 ────────────────────────────────────────────────────────────────
class ApproveBody(BaseModel):
    permission_level: str = "photos_stream"


def _load_for_decision(db: Session, req_id: int, current_user: User) -> CameraAccessRequest:
    req = db.query(CameraAccessRequest).filter(CameraAccessRequest.id == req_id).first()
    # 無權處理的人不該知道這筆請求存在 → 一律 404
    if not req or not _can_decide(db, current_user, req.camera_id):
        raise HTTPException(404, "請求不存在")
    if req.status != "pending":
        raise HTTPException(409, detail={"code": "REQUEST_ALREADY_DECIDED", "message": "這筆請求已經處理過了"})
    return req


@router.post("/{req_id}/approve")
async def approve_access_request(
    req_id: int,
    body: ApproveBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # async：LINE 推播要排進 running loop（同步 handler 跑在 threadpool，沒有 loop 可排）
    if body.permission_level not in PERMISSION_LEVELS:
        raise HTTPException(400, "未知的權限模式")
    req = _load_for_decision(db, req_id, current_user)
    requester = db.query(User).filter(User.id == req.requester_id).first()
    if not requester or not requester.is_active:
        raise HTTPException(400, "請求者的帳號已停用或不存在")

    access = db.query(CameraAccess).filter(
        CameraAccess.user_id == requester.id,
        CameraAccess.camera_id == req.camera_id,
    ).first()
    if access:
        access.permission_level = body.permission_level
        access.granted_by = current_user.id
        access.invitation_id = 0
    else:
        db.add(CameraAccess(
            camera_id=req.camera_id, user_id=requester.id, granted_by=current_user.id,
            permission_level=body.permission_level,
            invitation_id=0,  # 非邀請來源（哨兵，撤銷分享連結時不會被 NULL fallback 帶走）
        ))
    req.status = "approved"
    req.permission_level = body.permission_level
    req.decided_by = current_user.id
    req.decided_at = datetime.utcnow()
    # 從屬樹回填：與接受分享連結同一條規則
    if requester.role == "end_user" and not requester.reseller_id \
            and current_user.role in ("reseller", "symotus_admin"):
        requester.reseller_id = current_user.id
    log_action(db, current_user, "approve_camera_access_request", "camera_access_request", req.id,
               f"camera_id={req.camera_id} requester={requester.id} level={body.permission_level}")
    db.commit()

    notify_decision(db, requester.id, req.camera_name or f"相機 #{req.camera_id}", True, req.camera_id)
    return {"id": req.id, "status": "approved", "permission_level": body.permission_level}


@router.post("/{req_id}/reject")
async def reject_access_request(
    req_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    req = _load_for_decision(db, req_id, current_user)
    req.status = "rejected"
    req.decided_by = current_user.id
    req.decided_at = datetime.utcnow()
    log_action(db, current_user, "reject_camera_access_request", "camera_access_request", req.id,
               f"camera_id={req.camera_id} requester={req.requester_id}")
    db.commit()

    notify_decision(db, req.requester_id, req.camera_name or f"相機 #{req.camera_id}", False, req.camera_id)
    return {"id": req.id, "status": "rejected"}
