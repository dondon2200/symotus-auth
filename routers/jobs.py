import secrets
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel

from database import get_db
from models import User, TimelapsJob
from auth import (get_current_user, create_gdrive_oauth_ticket, decode_gdrive_oauth_ticket,
                  create_video_ticket, decode_video_ticket)
from schemas import UtcDatetime, utc_iso

router = APIRouter(prefix="/jobs", tags=["timelapse_jobs"])


class JobCreate(BaseModel):
    job_id: str
    camera_id: Optional[int] = None
    camera_name: Optional[str] = None
    serial_id: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    fps: Optional[int] = None
    resolution: Optional[str] = None

class JobUpdate(BaseModel):
    status: Optional[str] = None
    percent_complete: Optional[int] = None

class JobResponse(BaseModel):
    id: int
    job_id: str
    camera_id: Optional[int]
    camera_name: Optional[str]
    serial_id: Optional[str]
    status: str
    percent_complete: int
    start_date: Optional[str]
    end_date: Optional[str]
    fps: Optional[int]
    resolution: Optional[str]
    created_at: UtcDatetime
    updated_at: UtcDatetime

    class Config:
        from_attributes = True


@router.post("", response_model=JobResponse)
def create_job(
    body: JobCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 如果已存在就更新，不重複建立
    existing = db.query(TimelapsJob).filter(TimelapsJob.job_id == body.job_id).first()
    if existing:
        return existing

    job = TimelapsJob(
        user_id=current_user.id,
        job_id=body.job_id,
        camera_id=body.camera_id,
        camera_name=body.camera_name,
        serial_id=body.serial_id,
        start_date=body.start_date,
        end_date=body.end_date,
        fps=body.fps,
        resolution=body.resolution,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


@router.get("", response_model=list[JobResponse])
async def list_jobs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """列出相機縮時 jobs，並順手把未結束的 job 與 Spark 對齊。

    相機縮時的進度原本只有前端浮動卡片會回寫（TimelapsFloatingCard 輪詢 Spark
    後 PUT /jobs/{id}），使用者一關分頁就永遠定格——Spark 明明做完了，DB 還是
    processing/29%，也拿不到下載鈕。Spark 不會回呼 /jobs/internal（任務是相機
    後端提交的，callback_url 不指向本服務），所以在此比照 GET /jobs/gdrive/{id}
    的做法，於查詢時主動同步。Spark 查不到或逾時就沿用 DB 原值，不影響列表回應。
    """
    jobs = db.query(TimelapsJob).filter(
        TimelapsJob.user_id == current_user.id
    ).order_by(TimelapsJob.created_at.desc()).all()
    await _sync_jobs_with_spark(db, jobs)
    return jobs


@router.put("/{job_id}", response_model=JobResponse)
def update_job(
    job_id: str,
    body: JobUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = db.query(TimelapsJob).filter(
        TimelapsJob.job_id == job_id,
        TimelapsJob.user_id == current_user.id,
    ).first()
    if not job:
        raise HTTPException(404, "Job 不存在")
    if body.status is not None:
        # 只在「從非 completed 轉為 completed」時寫入 completed_at，避免重複
        # 輪詢到同一個結果時把完成日往後改，導致用量從舊日搬到新日。
        # 這裡沿用 utcnow() 而非 Spark 回報的真值：這個 endpoint 只在前端浮動
        # job 卡片開著的時候才會被打，drift 幅度被「使用者盯著看」這件事本身
        # 限制在頁面停留的時間內（通常幾分鐘），不像背景 list 同步那樣可能拖到幾天，
        # 可接受。
        if body.status == "completed" and job.status != "completed":
            job.completed_at = datetime.utcnow()
        job.status = body.status
    if body.percent_complete is not None:
        job.percent_complete = body.percent_complete
    job.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(job)
    return job


@router.delete("/{job_id}")
def delete_job(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = db.query(TimelapsJob).filter(
        TimelapsJob.job_id == job_id,
        TimelapsJob.user_id == current_user.id,
    ).first()
    if not job:
        raise HTTPException(404, "Job 不存在")
    db.delete(job)
    db.commit()
    return {"message": "已刪除"}


# ── Internal endpoint（Spark callback server-to-server）──────────────────
class JobInternalUpdate(BaseModel):
    status: Optional[str] = None
    percent_complete: Optional[int] = None
    video_url: Optional[str] = None
    error_message: Optional[str] = None
    image_count: Optional[int] = None
    processing_time_secs: Optional[str] = None
    # Spark 回呼若帶上真正的完成時間就用它；這個 endpoint 實務上從未被 Spark
    # 呼叫過（Spark 不會 callback），但保留欄位以防萬一，行為與 list 同步一致。
    completed_at: Optional[str] = None

@router.put("/internal/{job_id}")
def internal_update_job(
    job_id: str,
    body: JobInternalUpdate,
    request: "Request",
    db: Session = Depends(get_db),
):
    """給 Spark callback 用的 server-to-server endpoint，不需要 user token"""
    from fastapi import Request as FRequest
    service_key = request.headers.get("x-service-key")
    if service_key != "spark-callback":
        from fastapi import HTTPException
        raise HTTPException(403, "Invalid service key")

    job = db.query(TimelapsJob).filter(TimelapsJob.job_id == job_id).first()
    if not job:
        return {"message": "Job not found, ignored"}

    if body.status is not None:
        # 只在「從非 completed 轉為 completed」時寫入 completed_at，避免重複
        # 回呼同一結果時把完成日往後改，導致用量從舊日搬到新日。
        if body.status == "completed" and job.status != "completed":
            # 這個 endpoint 實務上從未被 Spark 呼叫過，但若真的帶上完成時間就
            # 優先採用（與 list 同步的邏輯一致），沒有才退回 utcnow()。
            job.completed_at = parse_spark_completed_at(body.completed_at, datetime.utcnow())
        job.status = body.status
    if body.percent_complete is not None: job.percent_complete = body.percent_complete
    if body.video_url is not None: job.video_url = body.video_url
    if body.error_message is not None: job.error_message = body.error_message
    if body.image_count is not None: job.image_count = body.image_count
    if body.processing_time_secs is not None: job.processing_time_secs = str(body.processing_time_secs)
    job.updated_at = datetime.utcnow()
    db.commit()
    return {"message": "Updated"}

# ── Google Drive 縮時影片（背景下載 + 直接送 Spark）──────────────────────────────

import asyncio
import os
import re
import time
import json
import struct
import logging
import httpx
from models import GDriveJob, GoogleDriveCredential
from config import settings

logger = logging.getLogger(__name__)

SPARK_API_URL = settings.SPARK_API_URL
SPARK_API_KEY = settings.SPARK_API_KEY
GDRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
# 相機縮時 job 查詢時同步 Spark 的上限：單筆逾時短、並發受限，避免列表 API 被拖慢
SPARK_SYNC_TIMEOUT = 6
SPARK_SYNC_CONCURRENCY = 4
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
# 並發路數。sem 同時夾住下載與寫檔，所以它也是「同時有幾張圖佔著記憶體」的上限。
#
# 實測基準（job 41 完整一輪，65541 張縮圖）：
#   48 路 → 11.9 張/s、11.7 MB/s，記憶體峰值 ~433 MiB
# 同一台 NFS 在 12 路搭 11 MB 原檔時跑到 42.8 MB/s，可見瓶頸不是頻寬而是
# 每檔的往返與 NFS metadata 開銷——那是靠並發數而非頻寬去攤平的，所以加倍。
#
# 記憶體成本：96 x ~1 MB ≈ 100 MB 在途緩衝，容器上限 2 GiB，有充裕餘裕。
# 縮圖走簽名 URL，不吃 Drive API 配額也不爭用 token 鎖，拉高是安全的。
# 若出現大量 429 或記憶體逼近上限，用 GDRIVE_DOWNLOAD_CONCURRENCY 調回 48。
DOWNLOAD_CONCURRENCY = int(os.getenv("GDRIVE_DOWNLOAD_CONCURRENCY", "96"))

# 縮圖下載：Drive 的 thumbnailLink 由 Google 端縮好，省掉傳輸用不到的像素。
# 實測 6000x4000（10.7 MB）的來源：=s2560 得 2560x1707（940 KB）、=s4096 得
# 4096x2731（1884 KB）。輸出 1080p/4K 都仍是「降採樣」，不損畫質。
THUMBNAIL_SIZES = {1920: 2560, 3840: 4096}   # 輸出寬 -> 要求的縮圖最長邊
THUMBNAIL_MIN_BYTES = 1024                   # 小於此視為錯誤頁而非圖片

# 每下載幾張回寫一次進度。commit 是同步的（持鎖時不能 await），間隔太小會頻繁
# 阻塞 event loop：71789 張以 25 為間隔要 2871 次。
PROGRESS_COMMIT_EVERY = 200

# Spark 單一資料夾的張數上限（超過回 422）。送件前一律抽樣到這個數以下，否則
# 會像 job 41 那樣下載完 65541 張、花掉一小時才被擋下來。
SPARK_MAX_IMAGES = int(os.getenv("SPARK_MAX_IMAGES", "50000"))

# 下載階段（本服務進程自己擁有）的狀態：進程一沒，這些 job 就是孤兒，必須回收成 interrupted。
# submitted/processing 不列入——那時工作已交給 Spark，重啟不影響它。
GDRIVE_OWNED_STATUSES = ("pending", "listing", "downloading")

# 正在跑的下載任務：job_id -> asyncio.Task。
# 兩個用途：(1) 關機時知道有哪些 job 要標成 interrupted；(2) 持有 Task 強引用，避免
# asyncio.create_task 的回傳值被 GC 回收導致任務中途消失。
_RUNNING_GDRIVE_TASKS: dict[int, asyncio.Task] = {}


# ── 相機縮時：查詢時與 Spark 對齊（供 list_jobs 用）─────────────────────────
# 定義在此是因為需要上方的 SPARK_API_URL/KEY 與 httpx；list_jobs 在呼叫時才解析
# 這個名稱，所以檔案前段的 endpoint 可以正常使用。

def _spark_status_to_local(raw: Optional[str]) -> Optional[str]:
    s = (raw or "").lower()
    if s in ("completed", "done", "success"):
        return "completed"
    if s in ("failed", "error"):
        return "failed"
    if s:
        return "processing"
    return None


def parse_spark_completed_at(raw, now: datetime) -> datetime:
    """把 Spark `GET /jobs/{id}` 回傳的 completed_at 解析成 naive UTC。

    - `raw` 為 None 或無法解析（非字串、格式錯誤）：回退用 `now`（呼叫端應記一筆
      log 說明這筆完成時間是估計值，不是 Spark 回報的真值）。
    - 帶時區資訊的字串（如 "+08:00" 或 "Z"）：換算成 UTC 後再去除 tzinfo，
      維持本專案「DB 一律存 naive UTC」的慣例。
    - 換算後若晚於 `now`（Spark 端鐘飄移），夾在 `now`，避免完成時間跑到未來。
    """
    if raw:
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            return min(parsed, now)
        except (ValueError, TypeError):
            pass
    return now


async def _sync_jobs_with_spark(db: Session, jobs: list) -> None:
    """把未結束的相機縮時 job 與 Spark 現況對齊並寫回 DB（就地更新傳入的 ORM 物件）。

    只碰 status 尚未終結的 job。任何 Spark 錯誤／逾時都靜默略過並保留 DB 原值，
    列表 API 不因外部服務不穩而失敗。
    """
    active = [j for j in jobs if (j.status or "").lower() not in ("completed", "failed")]
    if not active:
        return

    sem = asyncio.Semaphore(SPARK_SYNC_CONCURRENCY)

    async def fetch(job):
        async with sem:
            try:
                async with httpx.AsyncClient(timeout=SPARK_SYNC_TIMEOUT) as client:
                    r = await client.get(f"{SPARK_API_URL}/jobs/{job.job_id}",
                                         headers={"x-api-key": SPARK_API_KEY})
                if r.status_code == 200:
                    return job, r.json()
            except Exception:
                logger.debug("Spark 同步失敗（沿用 DB 值）: job_id=%s", job.job_id, exc_info=True)
            return job, None

    results = await asyncio.gather(*[fetch(j) for j in active])

    changed_any = False
    for job, data in results:
        if not data:
            continue
        status = _spark_status_to_local(data.get("status"))
        percent = data.get("percent_complete")
        changed = False
        # 用 lowercase 比較，跟上面 `active` 篩選（.lower()）一致：DB 若混雜
        # 大小寫（如 "Completed"），原本的原字串比較會誤判成「仍在轉態」，
        # 導致每次列表 API 都重寫 completed_at，正是本輪要修的用量日期漂移。
        if status and status != (job.status or "").lower():
            # 這裡本來就是「從非 completed 轉為 completed」的判定，
            # 所以 completed_at 只會在真正轉態時寫入，重複同步到同一結果不會覆蓋。
            if status == "completed":
                now = datetime.utcnow()
                raw_completed_at = data.get("completed_at")
                job.completed_at = parse_spark_completed_at(raw_completed_at, now)
                if job.completed_at == now:
                    # 代表 raw 缺漏或無法解析，parse_spark_completed_at 回退用了 now。
                    logger.info(
                        "job_id=%s: Spark completed_at 缺漏或無法解析（原始值=%r），"
                        "completed_at 採同步當下時間（估計值）",
                        job.job_id, raw_completed_at,
                    )
            job.status = status
            changed = True
        # 完成時補滿 100%，其餘採 Spark 回報值（只在數字有前進時才寫，避免倒退）
        if status == "completed":
            if job.percent_complete != 100:
                job.percent_complete = 100
                changed = True
        elif isinstance(percent, int) and percent > (job.percent_complete or 0):
            job.percent_complete = percent
            changed = True
        for field, value in (("image_count", data.get("image_count")),
                             ("error_message", data.get("error"))):
            if value is not None and getattr(job, field) != value:
                setattr(job, field, value)
                changed = True
        if changed:
            job.updated_at = datetime.utcnow()
            changed_any = True

    if changed_any:
        db.commit()


# ── OAuth：用消費者授權碼換 token、refresh token 續期 ──────────────────────────

async def _exchange_auth_code(auth_code: str, redirect_uri: str = "postmessage") -> dict:
    """用授權碼換 access + refresh token。

    redirect_uri 必須與取得授權碼時所用的一致：
    - GIS popup（舊路徑）：'postmessage'（GIS 慣例，不是 web OAuth 的 redirect URI）
    - 整頁 redirect（新路徑）：settings.GDRIVE_REDIRECT_URI
    """
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(GOOGLE_TOKEN_URL, data={
            "code": auth_code,
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        })
    if r.status_code != 200:
        raise HTTPException(400, f"Google 授權碼交換失敗（{r.status_code}）：{r.text[:200]}")
    return r.json()


class TransientGoogleError(RuntimeError):
    """Google 暫時性錯誤（429/5xx）：不代表授權已死，呼叫端應回 503 而非 409。

    刻意繼承 RuntimeError 是為了與既有的 `_TokenManager` 消費者相容（它們只認得
    RuntimeError）；但任何同時攔截兩者的 try/except，`except TransientGoogleError`
    必須排在 `except RuntimeError` 前面，否則永遠攔不到。
    """


async def _refresh_access_token(refresh_token: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(GOOGLE_TOKEN_URL, data={
            "refresh_token": refresh_token,
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "grant_type": "refresh_token",
        })
    if r.status_code == 429 or r.status_code == 403 or r.status_code >= 500:
        # Google 端暫時不可用（含 403 quota/使用者速率限制)，不代表 refresh token 本身失效
        raise TransientGoogleError(f"Google 暫時無法處理 refresh 請求（{r.status_code}）：{r.text[:200]}")
    if r.status_code != 200:
        # 真正的拒絕（400 invalid_grant、401 等）：refresh token 已死，需要重新同意
        raise RuntimeError(f"refresh token 失效（{r.status_code}）：{r.text[:200]}")
    return r.json()


class _TokenManager:
    """管理單一任務的 access token：將到期時用 refresh token 自動續期（asyncio.Lock 串行化）。

    沒有 refresh token 時只用初始 access token（短任務夠用，~1hr）；過期且無法續期則丟出
    明確錯誤，讓背景任務標記 job 失敗。
    """
    def __init__(self, refresh_token: Optional[str], access_token: Optional[str] = None, expires_in: int = 0):
        self._refresh_token = refresh_token
        self._access_token = access_token
        # 提早 120 秒視為過期，避免邊界 race
        self._expiry = (time.monotonic() + expires_in - 120) if access_token else 0.0
        self._lock = asyncio.Lock()

    async def _do_refresh(self):
        if not self._refresh_token:
            raise RuntimeError("access token 已過期且無 refresh token，無法續期")
        td = await _refresh_access_token(self._refresh_token)
        self._access_token = td["access_token"]
        self._expiry = time.monotonic() + td.get("expires_in", 3600) - 120

    async def get(self) -> str:
        async with self._lock:
            if not self._access_token or time.monotonic() >= self._expiry:
                await self._do_refresh()
            return self._access_token

    async def force_refresh(self) -> str:
        async with self._lock:
            await self._do_refresh()
            return self._access_token


async def _list_drive_images(token_mgr: "_TokenManager", folder_id: str,
                             max_images: Optional[int] = None,
                             _seen: Optional[set] = None, _depth: int = 0) -> list[dict]:
    """用消費者授權遞迴列出資料夾內所有圖片（drive.readonly scope）。

    遞迴展開子資料夾（最深 8 層），含 Shared Drive，以 file id 去重避免捷徑重複計算。
    """
    if _depth > 8:
        return []
    if _seen is None:
        _seen = set()

    images: list[dict] = []
    limit = max_images or 100_000
    FOLDER_MIME = "application/vnd.google-apps.folder"
    base_params = {
        "q": f"'{folder_id}' in parents and trashed=false",
        "fields": ("nextPageToken,files(id,name,mimeType,size,thumbnailLink,"
                   "shortcutDetails)"),
        "pageSize": "1000",
        "orderBy": "name",
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }

    page_token = None
    async with httpx.AsyncClient(timeout=30) as client:
        while len(images) < limit:
            access = await token_mgr.get()
            params = {**base_params}
            if page_token:
                params["pageToken"] = page_token

            resp = await client.get(GDRIVE_FILES_URL, params=params,
                                    headers={"Authorization": f"Bearer {access}"})
            if resp.status_code == 401:
                await token_mgr.force_refresh()
                continue
            if resp.status_code != 200:
                raise HTTPException(400, f"無法列出資料夾內容（{resp.status_code}）：{resp.text[:200]}")

            data = resp.json()
            for f in data.get("files", []):
                mime = f.get("mimeType", "")
                fid = f["id"]

                # 展開捷徑：取真實目標 id／mimeType
                if mime == "application/vnd.google-apps.shortcut":
                    sd = f.get("shortcutDetails", {})
                    fid = sd.get("targetId", fid)
                    mime = sd.get("targetMimeType", "")

                if fid in _seen:
                    continue
                _seen.add(fid)

                if mime == FOLDER_MIME:
                    sub = await _list_drive_images(token_mgr, fid, limit - len(images), _seen, _depth + 1)
                    images.extend(sub)
                elif mime.startswith("image/"):
                    images.append({"id": fid, "name": f["name"], "mimeType": mime,
                                   "thumbnailLink": f.get("thumbnailLink")})

            page_token = data.get("nextPageToken")
            if not page_token:
                break

    return images[:limit]


def _thumbnail_size_for(resolution: Optional[str]) -> Optional[int]:
    """依輸出解析度決定要跟 Google 要多大的縮圖；無法判斷就回 None（走原檔）。

    只有「縮圖尺寸 ≥ 輸出尺寸」才會啟用——否則等於拿上採樣的圖去生成影片，畫質會軟。
    """
    if not resolution:
        return None
    m = re.match(r"\s*(\d+)\s*[xX×]\s*(\d+)", resolution)
    if not m:
        return None
    return THUMBNAIL_SIZES.get(int(m.group(1)))


def _rewrite_thumbnail_url(link: str, size: int) -> str:
    """把 thumbnailLink 尾端的 =s220 之類改寫成要求的尺寸。"""
    if "=" in link.rsplit("/", 1)[-1]:
        return re.sub(r"=[^/]*$", f"=s{size}", link)
    return f"{link}=s{size}"


def _jpeg_width(data: bytes) -> Optional[int]:
    """從 JPEG 的 SOF 標頭讀出寬度，用來確認縮圖真的有要求的尺寸。

    Google 對某些檔案可能給不到要求的大小（例如來源本身就小），那時必須退回原檔，
    否則會拿上採樣的圖去輸出而使用者看不出原因。
    """
    try:
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            i += 2
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                continue
            seg_len = struct.unpack(">H", data[i:i + 2])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                return struct.unpack(">H", data[i + 5:i + 7])[0]
            i += seg_len
    except Exception:
        pass
    return None


async def _fresh_thumbnail_link(session: httpx.AsyncClient, token_mgr: "_TokenManager",
                                file_id: str) -> Optional[str]:
    """重新取得單一檔案的 thumbnailLink。

    thumbnailLink 是短效簽名 URL，列檔到下載完可能跨數十分鐘，過期後整批會 403。
    與其猜它的壽命再分批 re-list，不如在失敗當下換一條新的——自癒且不必知道壽命。
    """
    try:
        access = await token_mgr.get()
        r = await session.get(f"{GDRIVE_FILES_URL}/{file_id}",
                              params={"fields": "thumbnailLink", "supportsAllDrives": "true"},
                              headers={"Authorization": f"Bearer {access}"})
        if r.status_code == 200:
            return r.json().get("thumbnailLink")
    except Exception:
        pass
    return None


async def _download_thumbnail(session: httpx.AsyncClient, token_mgr: "_TokenManager",
                              item: dict, size: int, min_width: int) -> Optional[bytes]:
    """下載指定尺寸的縮圖；任何不確定的情況都回 None 讓呼叫端退回原檔。

    縮圖是簽名 URL，不需要（也不該）帶 Authorization——省掉 token 鎖的爭用，
    這正是能把並發拉到數十路的原因。
    """
    link = item.get("thumbnailLink")
    for attempt in range(2):
        if not link:
            link = await _fresh_thumbnail_link(session, token_mgr, item["id"])
            if not link:
                return None
        try:
            resp = await session.get(_rewrite_thumbnail_url(link, size), follow_redirects=True)
        except Exception:
            link = None
            continue
        if resp.status_code == 200 and len(resp.content) >= THUMBNAIL_MIN_BYTES:
            width = _jpeg_width(resp.content)
            if width is not None and width < min_width:
                return None      # 尺寸不足：退回原檔，不能拿上採樣的圖充數
            item["thumbnailLink"] = link
            return resp.content
        # 403/404/410 多半是簽名過期 → 換一條新的再試一次
        link = None
    return None


async def _fetch_and_store(session: httpx.AsyncClient, token_mgr: "_TokenManager",
                           sem: asyncio.Semaphore, item: dict, dest_path: str,
                           thumb_size: Optional[int], out_width: int, stats: dict) -> None:
    """抓一張圖並寫進 NAS。下載與寫檔都在 sem 之內。

    sem 是「同時有幾張圖佔著記憶體」的唯一上限，所以寫檔不能移到 sem 之外——那樣
    下載（快、數十路）會遠遠超前 NFS 寫入，每張未寫出的圖都以 bytes 留在 RAM。
    """
    async with sem:
        data = None
        if thumb_size:
            data = await _download_thumbnail(session, token_mgr, item, thumb_size, out_width)
        if data is not None:
            stats["thumb"] = stats.get("thumb", 0) + 1
        else:
            data = await _download_file_api(session, token_mgr, item["id"])
            if data is not None:
                stats["orig"] = stats.get("orig", 0) + 1
        if data:
            stats["bytes"] = stats.get("bytes", 0) + len(data)
            await asyncio.to_thread(_write_file, dest_path, data)


def _write_file(path: str, data: bytes):
    with open(path, "wb") as fp:
        fp.write(data)


def _existing_sizes(nas_path: str) -> dict[str, int]:
    """列出目錄內既有檔案的 name -> size，供續傳時跳過已下載的照片。

    只認 size > 0 的檔案；大小為 0 的殘檔（上次被砍在寫入當下）會被重新下載覆蓋。
    NFS 上 7 萬筆 scandir 約數秒，只在下載開始前做一次。
    """
    sizes: dict[str, int] = {}
    try:
        with os.scandir(nas_path) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                try:
                    if e.is_file():
                        sizes[e.name] = e.stat().st_size
                except OSError:
                    continue
    except FileNotFoundError:
        pass
    return sizes


async def _download_file_api(session: httpx.AsyncClient, token_mgr: "_TokenManager", file_id: str) -> Optional[bytes]:
    """用消費者權限 + Drive API v3 files.get?alt=media 下載單張圖片（高配額、不限速、不跳病毒掃描頁）。"""
    url = f"{GDRIVE_FILES_URL}/{file_id}"
    params = {"alt": "media", "supportsAllDrives": "true"}
    for attempt in range(4):
        try:
            access = await token_mgr.get()
            resp = await session.get(url, params=params,
                                     headers={"Authorization": f"Bearer {access}"},
                                     follow_redirects=True, timeout=120)
            if resp.status_code == 200 and len(resp.content) > 0:
                return resp.content
            if resp.status_code == 401:
                await token_mgr.force_refresh()
                continue
            if resp.status_code in (403, 429, 500, 502, 503) and attempt < 3:
                await asyncio.sleep(2 ** attempt)  # 1s, 2s, 4s 退避
                continue
            return None
        except Exception:
            if attempt < 3:
                await asyncio.sleep(2 ** attempt)
    return None




def _sample_by_day(nas_path: str, frames_needed: int):
    """下載完之後，按天平均抽取照片，確保每天都有幀出現在影片中。
    抽完的照片保留，其餘從 NAS 刪除，再送 Spark。

    frames_needed 是目標張數（時長 x fps，或 Spark 的硬上限）。已經不多於目標時
    直接返回，所以重複呼叫是安全的（續傳會用到這個性質）。
    """
    from collections import defaultdict

    all_files = sorted([
        f for f in os.listdir(nas_path)
        if not f.startswith(".") and os.path.isfile(os.path.join(nas_path, f))
    ])
    total = len(all_files)

    if total <= frames_needed:
        return  # 不需要抽取

    # 嘗試從檔名解析日期（YYYYMMDD 或 YYYY-MM-DD 或 YYYY_MM_DD）
    date_pattern = re.compile(r'(\d{4})[_\-]?(\d{2})[_\-]?(\d{2})')
    by_day = defaultdict(list)
    no_date = []
    for f in all_files:
        m = date_pattern.search(f)
        if m:
            by_day[f"{m.group(1)}{m.group(2)}{m.group(3)}"].append(f)
        else:
            no_date.append(f)

    if not by_day:
        # 沒找到日期 → 全局等間距抽取
        step = total / frames_needed
        keep = set(all_files[int(i * step)] for i in range(frames_needed))
    else:
        days = sorted(by_day.keys())
        frames_per_day = max(1, frames_needed // len(days))
        keep = set()
        for day in days:
            day_files = sorted(by_day[day])
            n = len(day_files)
            if n <= frames_per_day:
                keep.update(day_files)
            else:
                step = n / frames_per_day
                keep.update(day_files[int(i * step)] for i in range(frames_per_day))

    # 按天分配保證「每天都有」，但不保證總數：天數比目標張數多時，每天各留一張
    # 就已經超出目標。這裡再等間距壓一次，讓「不超過 frames_needed」成為硬保證——
    # Spark 的張數上限靠這個函式把關，超出就是 422 白跑一趟。
    if len(keep) > frames_needed:
        ordered = sorted(keep)
        step = len(ordered) / frames_needed
        keep = set(ordered[int(i * step)] for i in range(frames_needed))

    # 刪除不在 keep 集合裡的檔案
    for f in all_files:
        if f not in keep:
            try:
                os.remove(os.path.join(nas_path, f))
            except Exception:
                pass


async def _run_gdrive_nas_pipeline(job_id: int, folder_ids: list[str], picked_files: list[dict],
                                   refresh_token: Optional[str],
                                   body_fps: int, body_resolution, rain_fog: bool, darkness: bool,
                                   max_images=None, initial_access_token: Optional[str] = None,
                                   initial_expires_in: int = 0, duration_seconds: Optional[int] = None,
                                   image_recovery: bool = False):
    """背景任務：用消費者自己的 Drive 授權並發下載 → NAS → Spark /jobs/nas。

    下載清單 = 個別選取的照片（picked_files）+ 各選取資料夾遞迴展開的照片（drive.readonly，含
    子資料夾、Shared Drive、捷徑，依 id 去重）。
    走 Drive API v3 files.get?alt=media（per-user 配額、不限速），並發 DOWNLOAD_CONCURRENCY 路；
    token 到期用 refresh token 自動續期。NAS→Spark 段沿用。
    """
    from database import SessionLocal
    db = SessionLocal()
    nas_folder = f"gdrive_{job_id}"
    nas_path = f"/homes/firmness/{nas_folder}"
    sampled_marker = os.path.join(nas_path, ".sampled")
    token_mgr = _TokenManager(refresh_token, initial_access_token, initial_expires_in)
    try:
        import shutil
        job = db.query(GDriveJob).filter(GDriveJob.id == job_id).first()
        if not job: return

        # 續傳：保留既有目錄，已下載的檔案在下面逐一比對後跳過。
        # （舊行為是先 rmtree 整個目錄，任何中斷都要從 0 重來——7 萬張／790 GB 的任務
        #   在一次部署面前必然歸零，這正是 job 40 卡住的根因。）
        os.makedirs(nas_path, exist_ok=True)

        # 1. 建下載清單：個別照片 + 各資料夾列出的照片（依 id 去重）
        job.status = "listing"; db.commit()
        download: list[dict] = []
        seen: set[str] = set()
        for it in picked_files:
            if it.get("id") and it["id"] not in seen:
                # Picker 選的個別照片沒有 thumbnailLink，下載時再現取
                seen.add(it["id"]); download.append({"id": it["id"], "name": it.get("name") or it["id"],
                                                     "thumbnailLink": None})
        try:
            for folder_id in folder_ids:
                for f in await _list_drive_images(token_mgr, folder_id, max_images):
                    if f["id"] not in seen:
                        seen.add(f["id"]); download.append({"id": f["id"], "name": f["name"],
                                                            "thumbnailLink": f.get("thumbnailLink")})
        except Exception as e:
            # 列檔失敗多半是暫時性的（Google 503／網路），已下載的檔案要留著給續傳用
            job.status = "interrupted"; job.error_message = f"無法讀取資料夾：{e}"; db.commit()
            return
        if not download:
            job.status = "failed"; job.error_message = "選取的項目中沒有找到圖片"; db.commit()
            shutil.rmtree(nas_path, ignore_errors=True); return
        job.total_images = len(download); db.commit()

        # 2. 並發下載到 NAS（Drive API alt=media）。檔名加序號前綴：保序＋避免同名覆蓋。
        #    續傳：同名且非空的檔案直接跳過，只補齊缺的部分。
        job.status = "downloading"; db.commit()
        existing = await asyncio.to_thread(_existing_sizes, nas_path)
        sem = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)
        progress = {"done": 0}
        progress_lock = asyncio.Lock()

        def _dest_name(idx: int, item: dict) -> str:
            safe = str(item["name"]).replace("/", "_").replace("\\", "_")
            return f"{idx:06d}_{safe}"

        # 已抽樣過的目錄不能再依清單補檔——被抽掉的照片是「刻意刪除」，不是「還沒下載」。
        if os.path.exists(sampled_marker):
            todo = []
            logger.info("gdrive job %s 續傳：已完成下載與抽樣，直接重送 Spark", job_id)
        else:
            todo = [(i, it) for i, it in enumerate(download) if existing.get(_dest_name(i, it), 0) <= 0]
        skipped = len(download) - len(todo)
        progress["done"] = skipped
        job.downloaded_count = skipped; db.commit()
        if skipped:
            logger.info("gdrive job %s 續傳：已有 %s 張，尚需下載 %s 張", job_id, skipped, len(todo))

        # 縮圖尺寸：只在「縮圖 ≥ 輸出」時啟用。AI 影像修復是生成式增強，輸入像素越多
        # 越好，4K 搭配它時不冒這個險，一律走原檔。
        thumb_size = _thumbnail_size_for(body_resolution)
        _m = re.match(r"\s*(\d+)", body_resolution or "")
        out_width = int(_m.group(1)) if _m else 0
        if thumb_size and image_recovery and out_width >= 3840:
            thumb_size = None
            logger.info("gdrive job %s：4K + AI 影像修復 → 停用縮圖下載，改走原檔", job_id)
        if thumb_size:
            logger.info("gdrive job %s：啟用縮圖下載 =s%s（輸出 %s）", job_id, thumb_size, body_resolution)
        stats = {"thumb": 0, "orig": 0, "bytes": 0}

        limits = httpx.Limits(max_connections=DOWNLOAD_CONCURRENCY * 2,
                              max_keepalive_connections=DOWNLOAD_CONCURRENCY * 2)
        async with httpx.AsyncClient(timeout=120, limits=limits) as session:
            async def fetch_one(idx: int, item: dict):
                await _fetch_and_store(session, token_mgr, sem, item,
                                       os.path.join(nas_path, _dest_name(idx, item)),
                                       thumb_size, out_width, stats)
                async with progress_lock:
                    progress["done"] += 1
                    if progress["done"] % PROGRESS_COMMIT_EVERY == 0:
                        job.downloaded_count = progress["done"]
                        # 同步 commit：持鎖時不可 await。先前用 asyncio.to_thread 讓這裡
                        # 排到共用執行緒池的寫檔佇列後面，進度因此凍結在 6550 不動。
                        db.commit()
            t0 = time.monotonic()
            await asyncio.gather(*[fetch_one(i, it) for i, it in todo])
            elapsed = max(time.monotonic() - t0, 0.001)
            logger.info("gdrive job %s 下載完成：縮圖 %s／原檔 %s，%.1f GB，%.0fs（%.1f MB/s）",
                        job_id, stats["thumb"], stats["orig"], stats["bytes"] / 1e9,
                        elapsed, stats["bytes"] / 1e6 / elapsed)

        saved = len([x for x in os.listdir(nas_path) if not x.startswith(".")])
        job.downloaded_count = saved; db.commit()
        if saved < 10:
            job.status = "failed"; job.error_message = f"只下載到 {saved} 張"; db.commit()
            shutil.rmtree(nas_path, ignore_errors=True); return

        # 2.5 按天抽取照片。目標張數取兩個來源的較小值：
        #   - 使用者指定的時長（duration_seconds x fps）
        #   - Spark 的硬上限：超過就是 422，白下載一場（job 41 下載完 65541 張才被擋）
        # 抽樣是破壞性的（刪檔），所以做完留下 marker：續傳時下載階段要靠它判斷
        # 「檔案少是因為抽過」而不是「還沒下載完」，否則會把抽掉的照片全部重抓。
        target = duration_seconds * body_fps if (duration_seconds and duration_seconds > 0) else None
        if saved > SPARK_MAX_IMAGES:
            target = min(target, SPARK_MAX_IMAGES) if target else SPARK_MAX_IMAGES
            logger.info("gdrive job %s：%s 張超過 Spark 上限 %s，抽樣至 %s 張",
                        job_id, saved, SPARK_MAX_IMAGES, target)
        if target:
            await asyncio.to_thread(_sample_by_day, nas_path, target)
            await asyncio.to_thread(_write_file, sampled_marker, b"")
            saved = len([x for x in os.listdir(nas_path) if not x.startswith(".")])
            job.downloaded_count = saved; db.commit()

        # 3. Spark 從 NAS 讀（無大小限制）
        job.status = "submitted"; db.commit()
        callback_url = f"{settings.PUBLIC_BASE_URL}/jobs/gdrive/callback/{job_id}"
        async with httpx.AsyncClient(timeout=30) as client:
            sr = await client.post(f"{SPARK_API_URL}/jobs/nas",
                headers={"x-api-key": SPARK_API_KEY},
                json={"nas_path": nas_folder, "callback_url": callback_url,
                      "fps": body_fps, "resolution": body_resolution,
                      "rain_fog_detection": rain_fog, "darkness_detection": darkness})
        if sr.status_code not in (200, 202):
            # 照片一律留在 NAS：重下載可能是數十 GB／數小時，不能因為送件失敗就丟掉。
            # 但狀態要分流——4xx 是請求本身不合法（例如 422 張數超上限），原封重送
            # 只會再失敗一次；把它標成 interrupted 等於請使用者按一個註定無效的按鈕。
            transient = sr.status_code == 429 or sr.status_code >= 500
            job.status = "interrupted" if transient else "failed"
            job.error_message = f"Spark 錯誤（{sr.status_code}）：{sr.text[:200]}"
            db.commit()
            return
        job.spark_job_id = str(sr.json().get("job_id", ""))
        job.status = "processing"; db.commit()
    except Exception as e:
        try:
            job = db.query(GDriveJob).filter(GDriveJob.id == job_id).first()
            if job: job.status = "failed"; job.error_message = str(e)[:300]; db.commit()
        except Exception: pass
    finally:
        db.close()


# ── 下載任務的生命週期管理（P0-b：孤兒回收 + 優雅關閉）──────────────────────────

def _launch_gdrive_pipeline(job_id: int, **kwargs) -> None:
    """啟動下載任務並登記到 _RUNNING_GDRIVE_TASKS。

    一定要透過這個函式啟動，不要直接 asyncio.create_task：登記表同時負責持有 Task
    的強引用（否則 event loop 只保留弱引用，任務可能被 GC 掉）與關機時的回收。
    """
    task = asyncio.create_task(_run_gdrive_nas_pipeline(job_id, **kwargs))
    _RUNNING_GDRIVE_TASKS[job_id] = task
    task.add_done_callback(lambda _t, jid=job_id: _RUNNING_GDRIVE_TASKS.pop(jid, None))


def _mark_interrupted(db: Session, job_ids: Optional[list[int]] = None) -> int:
    """把仍由本進程負責的下載階段 job 標成 interrupted（可續傳）。

    job_ids 為 None 代表「這個 DB 裡所有處於下載階段的 job」——啟動時用，因為新進程
    不可能擁有任何舊任務，它們必然是上一個進程留下的孤兒。
    """
    q = db.query(GDriveJob).filter(GDriveJob.status.in_(GDRIVE_OWNED_STATUSES))
    if job_ids is not None:
        if not job_ids:
            return 0
        q = q.filter(GDriveJob.id.in_(job_ids))
    jobs = q.all()
    for job in jobs:
        job.status = "interrupted"
        job.error_message = "服務重啟導致下載中斷，已下載的照片仍保留，可直接續傳"
    if jobs:
        db.commit()
    return len(jobs)


def reap_orphaned_gdrive_jobs() -> int:
    """啟動時呼叫：回收上一個進程留下的孤兒下載任務。

    下載任務是 asyncio.create_task 跑在 uvicorn 進程內、沒有佇列也沒有持久化的，
    進程一被換掉（部署／SIGKILL）就無聲消失，job 會永遠停在 downloading。這裡把它們
    收斂成 interrupted，使用者才看得到並能續傳。
    """
    from database import SessionLocal
    db = SessionLocal()
    try:
        n = _mark_interrupted(db)
        if n:
            logger.warning("回收 %s 個上次未完成的 GDrive 下載任務（標記為 interrupted）", n)
        return n
    except Exception:
        logger.exception("回收孤兒 GDrive 任務失敗")
        return 0
    finally:
        db.close()


async def shutdown_gdrive_jobs() -> None:
    """關機時呼叫：先把進行中的下載標成 interrupted 再讓進程結束。

    Docker SIGTERM 預設有 10 秒寬限，寫一筆 DB 綽綽有餘。這是「正常關機」的快樂路徑；
    SIGKILL 或斷電走不到這裡，由 reap_orphaned_gdrive_jobs() 在下次啟動時兜底。
    """
    job_ids = list(_RUNNING_GDRIVE_TASKS.keys())
    if not job_ids:
        return
    from database import SessionLocal
    db = SessionLocal()
    try:
        n = await asyncio.to_thread(_mark_interrupted, db, job_ids)
        logger.warning("關機：已將 %s 個進行中的 GDrive 下載標記為 interrupted", n)
    except Exception:
        logger.exception("關機標記 GDrive 任務失敗")
    finally:
        db.close()
    for jid in job_ids:
        task = _RUNNING_GDRIVE_TASKS.get(jid)
        if task and not task.done():
            task.cancel()


class FileRef(BaseModel):
    id: str
    name: Optional[str] = None


# ── GDrive OAuth：整頁 redirect 授權 ──────────────────────────────────────────

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
GDRIVE_STATE_COOKIE = "gdrive_oauth_state"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


@router.get("/gdrive/oauth/url")
def gdrive_oauth_url(
    response: Response,
    current_user: User = Depends(get_current_user),
):
    """回傳 Google 同意畫面網址，前端直接整頁導向過去（不開彈窗）。

    state 同時做兩件事：夾帶簽章 ticket 讓 callback 認得使用者，
    以及寫進 HttpOnly cookie 供 callback 做 round-trip 比對防 CSRF。
    """
    if not (settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET):
        raise HTTPException(500, "伺服器未設定 Google OAuth 憑證（GOOGLE_CLIENT_ID/SECRET）")

    state = create_gdrive_oauth_ticket(current_user.id)
    response.set_cookie(GDRIVE_STATE_COOKIE, state, max_age=600,
                        httponly=True, secure=True, samesite="lax", path="/")
    params = urlencode({
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.GDRIVE_REDIRECT_URI,
        "response_type": "code",
        "scope": DRIVE_SCOPE,
        "access_type": "offline",
        "prompt": "consent",   # 強制同意，確保每次都拿得到 refresh_token
        "state": state,
    })
    return {"auth_url": f"{GOOGLE_AUTH_URL}?{params}"}


GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


async def _fetch_google_email(access_token: str) -> Optional[str]:
    """取 Google 帳號 email 純粹是為了在前端顯示「已連接 xxx@gmail.com」。
    拿不到不影響授權，回 None 即可。"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(GOOGLE_USERINFO_URL,
                                 headers={"Authorization": f"Bearer {access_token}"})
        if r.status_code == 200:
            return r.json().get("email")
    except Exception:
        logger.debug("取 Google userinfo 失敗（不影響授權）", exc_info=True)
    return None


async def _upsert_drive_credential(db: Session, user_id: int, refresh_token: str,
                                   google_email: Optional[str], scope: Optional[str]) -> None:
    """一位使用者一組憑證：有就更新，沒有才新增。

    再次同意（re-consent）時刻意不撤銷既有的 refresh_token：Google 的 revoke 端點
    撤的是整組 authorization grant，不是單一 token 字串。consent URL 帶了
    prompt=consent，重新同意會核發「同一組 grant」下的新 refresh_token；若在覆寫前
    撤銷舊 token，等於連新拿到的這組 grant 一起拆掉——新 token 寫進 DB 後首次拿
    picker token 就會 invalid_grant → 409 → 前端清空綁定 → 使用者重連 → 無限迴圈。
    只有 disconnect endpoint（使用者主動要求解除綁定）才應該呼叫 _revoke_google_token。
    """
    row = db.query(GoogleDriveCredential).filter(
        GoogleDriveCredential.user_id == user_id
    ).first()
    if row:
        row.refresh_token = refresh_token
        row.google_email = google_email
        row.scope = scope
        row.updated_at = datetime.utcnow()
    else:
        db.add(GoogleDriveCredential(
            user_id=user_id, refresh_token=refresh_token,
            google_email=google_email, scope=scope,
        ))
    db.commit()


@router.get("/gdrive/oauth/callback")
async def gdrive_oauth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    db: Session = Depends(get_db),
):
    """Google 授權完成後導回這裡（top-level GET，沒有 Authorization header）。

    身分來自 state 裡的簽章 ticket；防 CSRF 靠 state 與 cookie 的 round-trip 比對。
    無論成敗都清掉 cookie，並 307 回前端 /gdrive 帶上結果。
    """
    frontend = settings.FRONTEND_URL

    def _redirect(query: str) -> RedirectResponse:
        resp = RedirectResponse(f"{frontend}/gdrive?{query}")
        resp.delete_cookie(GDRIVE_STATE_COOKIE, path="/",
                           httponly=True, secure=True, samesite="lax")
        return resp

    if error:
        # 使用者在同意畫面按取消：不是錯誤，靜默返回
        return _redirect("gdrive=cancelled")

    saved_state = request.cookies.get(GDRIVE_STATE_COOKIE)
    try:
        state_ok = bool(state) and bool(saved_state) and secrets.compare_digest(state, saved_state)
    except (TypeError, ValueError):
        # compare_digest 只接受 ASCII str/bytes；非 ASCII 的 state 視同不符
        state_ok = False
    if not state_ok:
        return _redirect("gdrive=error&reason=state")

    user_id = decode_gdrive_oauth_ticket(state)
    if not user_id:
        return _redirect("gdrive=error&reason=state")

    try:
        td = await _exchange_auth_code(code, settings.GDRIVE_REDIRECT_URI)
    except (HTTPException, httpx.HTTPError, ValueError):
        logger.warning("GDrive redirect 授權碼交換失敗", exc_info=True)
        return _redirect("gdrive=error&reason=exchange")

    if not isinstance(td, dict):
        logger.warning("GDrive redirect 授權碼交換回傳非預期格式（%r）", type(td))
        return _redirect("gdrive=error&reason=exchange")

    refresh_token = td.get("refresh_token")
    access_token = td.get("access_token")
    if not refresh_token:
        # 帶了 prompt=consent 仍拿不到，代表無法長期綁定，直接視為失敗
        logger.warning("GDrive redirect 授權未取得 refresh_token（user_id=%s）", user_id)
        return _redirect("gdrive=error&reason=no_refresh_token")

    email = await _fetch_google_email(access_token) if access_token else None
    try:
        await _upsert_drive_credential(db, user_id, refresh_token, email, td.get("scope"))
    except Exception:
        logger.warning("GDrive 憑證寫入失敗（user_id=%s）", user_id, exc_info=True)
        db.rollback()
        return _redirect("gdrive=error&reason=store")
    return _redirect("gdrive=connected")


GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"


async def _revoke_google_token(token: str) -> None:
    """向 Google 撤銷授權。失敗不擋流程——本地紀錄還是要刪掉。

    log 等級用 warning：撤銷失敗代表舊授權可能仍活在 Google 端（隱私後果），
    不該被 debug 等級悄悄吞掉。
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(GOOGLE_REVOKE_URL, data={"token": token})
    except Exception:
        logger.warning("Google token revoke 失敗（仍會刪除本地憑證）", exc_info=True)


def _video_download_url(job: GDriveJob) -> Optional[str]:
    """產生不含祕鑰的影片下載連結。

    絕對不要把 Spark 的 URL 直接交給瀏覽器：那個連結長成
    `.../download?api_key=<SPARK_API_KEY>`，等於把 Spark 的完整 API 權限
    交出去（分享連結、瀏覽器紀錄、Referer 都會外流）。改成指向本服務的代理
    端點，由後端持 key 去取檔，前端只拿到一張綁 user+job 的短效 ticket。
    """
    if not job.spark_job_id:
        return None
    ticket = create_video_ticket(job.user_id, job.id)
    return f"{settings.PUBLIC_BASE_URL}/jobs/gdrive/{job.id}/video?t={ticket}"


@router.get("/gdrive/{job_id}/video")
async def download_gdrive_video(job_id: int, t: str, db: Session = Depends(get_db)):
    """代理下載已生成的影片，避免把 SPARK_API_KEY 曝露給瀏覽器。

    身分靠 query string 的短效 ticket——瀏覽器的 top-level 下載導覽帶不了
    Authorization header。ticket 同時綁定 user 與 job，所以不能拿別人的 ticket
    換這支影片，也不能拿這支的 ticket 換別支。
    """
    claims = decode_video_ticket(t)
    if not claims:
        raise HTTPException(403, "下載連結已過期，請重新整理頁面")
    user_id, ticket_job_id = claims
    if ticket_job_id != job_id:
        raise HTTPException(403, "下載連結與此任務不符")

    job = db.query(GDriveJob).filter(
        GDriveJob.id == job_id,
        GDriveJob.user_id == user_id,
    ).first()
    if not job or not job.spark_job_id:
        raise HTTPException(404, "找不到此任務的影片")

    upstream = f"{SPARK_API_URL}/jobs/{job.spark_job_id}/download"
    client = httpx.AsyncClient(timeout=None)
    try:
        req = client.build_request("GET", upstream, headers={"x-api-key": SPARK_API_KEY})
        resp = await client.send(req, stream=True)
    except Exception:
        await client.aclose()
        raise HTTPException(502, "暫時無法取得影片，請稍後再試")
    if resp.status_code != 200:
        await resp.aclose(); await client.aclose()
        raise HTTPException(502, f"Spark 取檔失敗（{resp.status_code}）")

    async def body():
        # 串流轉送：影片可能數百 MB，不能整份讀進記憶體再回傳
        try:
            async for chunk in resp.aiter_bytes(1 << 16):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    headers = {"Content-Disposition": f'attachment; filename="timelapse_{job_id}.mp4"'}
    if resp.headers.get("content-length"):
        headers["Content-Length"] = resp.headers["content-length"]
    return StreamingResponse(body(),
                             media_type=resp.headers.get("content-type", "video/mp4"),
                             headers=headers)


def _get_drive_credential(db: Session, user_id: int) -> Optional[GoogleDriveCredential]:
    return db.query(GoogleDriveCredential).filter(
        GoogleDriveCredential.user_id == user_id
    ).first()


@router.get("/gdrive/oauth/status")
def gdrive_oauth_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """前端進 /gdrive 時先問這支，決定要顯示「連接」還是直接開 Picker。"""
    cred = _get_drive_credential(db, current_user.id)
    return {"connected": bool(cred), "google_email": cred.google_email if cred else None}


@router.post("/gdrive/oauth/token")
async def gdrive_picker_token(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """代發一份短效 access token 給 Google Picker 用。

    前端因此不必自己跑 OAuth（那會開彈窗），只需要這一次 API 呼叫。
    """
    cred = _get_drive_credential(db, current_user.id)
    if not cred:
        raise HTTPException(404, "尚未連接 Google Drive")
    try:
        td = await _refresh_access_token(cred.refresh_token)
    except TransientGoogleError:
        logger.warning("GDrive refresh 暫時無法處理（user_id=%s）", current_user.id, exc_info=True)
        raise HTTPException(503, "暫時無法連上 Google，請稍後再試")
    except RuntimeError:
        logger.warning("GDrive refresh token 失效（user_id=%s）", current_user.id, exc_info=True)
        raise HTTPException(409, "Google 授權已失效，請重新連接 Google Drive")
    except httpx.HTTPError:
        logger.warning("GDrive refresh 暫時無法連上 Google（user_id=%s）", current_user.id, exc_info=True)
        raise HTTPException(503, "暫時無法連上 Google，請稍後再試")
    return {"access_token": td.get("access_token"), "expires_in": td.get("expires_in", 0)}


@router.delete("/gdrive/oauth")
async def gdrive_oauth_disconnect(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """解除連接：向 Google 撤銷並刪掉本地憑證。未連接時視為已完成。"""
    cred = _get_drive_credential(db, current_user.id)
    if not cred:
        return {"revoked": False}
    await _revoke_google_token(cred.refresh_token)
    db.delete(cred)
    db.commit()
    return {"revoked": True}


class GDriveJobRequest(BaseModel):
    folder_id: Optional[str] = None          # 向後相容：單一資料夾
    folder_name: Optional[str] = None
    folder_ids: Optional[list[str]] = None   # 多選：資料夾（後端列出內含照片）
    files: Optional[list[FileRef]] = None    # 多選：個別照片
    selection_name: Optional[str] = None     # 顯示用（如「2 個資料夾、30 張照片」）
    auth_code: Optional[str] = None   # 舊 popup 流程才會帶；redirect 流程改用綁定憑證
    fps: int = 30
    resolution: Optional[str] = "1920x1080"
    rain_fog_detection: bool = False
    darkness_detection: bool = False
    image_recovery: bool = False
    stabilization: bool = False
    max_images: Optional[int] = None
    duration_seconds: Optional[int] = None


@router.post("/gdrive")
async def create_gdrive_job(
    body: GDriveJobRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """從 Google Drive 建立縮時影片 job（OAuth + Picker 流程，支援多選）。

    有兩條取得 token 的路徑，皆用消費者自己的權限並發下載 Picker 選到的「照片與/或
    資料夾」→ NAS（/homes/firmness/gdrive_<id>）→ 呼叫 Spark POST /jobs/nas，不再爬
    公開連結：

    - 正常路徑（整頁 redirect）：用戶已透過 `/gdrive/oauth/url` 完成整頁授權並綁定
      `GoogleDriveCredential`，本次呼叫直接用該長期 refresh token 換一份新的 access
      token（`_refresh_access_token`）。refresh 失敗依錯誤型別分類：`RuntimeError`
      （token 失效）回 409 請用戶重新連接；`httpx.HTTPError`（Google 暫時不可連）回
      503；其餘例外視為真的伺服器錯誤，原樣往外拋成 500。
    - 舊路徑（`auth_code`，GIS popup 相容）：舊前端仍會在請求帶上 GIS code client
      取得的 offline 授權碼，後端當場用 `redirect_uri='postmessage'` 換 refresh +
      access token。僅為 auth-service 與前端分階段部署期間保留的相容分支，前端全面
      改用綁定憑證後可移除。
    """
    folder_ids = list(body.folder_ids or [])
    if body.folder_id:
        folder_ids.append(body.folder_id)
    picked_files = [{"id": f.id, "name": f.name} for f in (body.files or []) if f.id]
    if not folder_ids and not picked_files:
        raise HTTPException(400, "未選取任何資料夾或照片")
    if not (settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET):
        raise HTTPException(500, "伺服器未設定 Google OAuth 憑證（GOOGLE_CLIENT_ID/SECRET）")

    if body.auth_code:
        # 舊路徑（GIS popup）：授權碼當場換 token，以 redirect_uri='postmessage' 交換
        td = await _exchange_auth_code(body.auth_code)
        access_token = td.get("access_token")
        refresh_token = td.get("refresh_token")
        expires_in = td.get("expires_in", 0)
        if not access_token:
            raise HTTPException(400, "Google 授權交換未取得 access token")
        if not refresh_token:
            # 用戶若先前已授權，Google 可能不再回 refresh token；短任務用 access token 仍可完成，
            # 但超過 token 壽命的長任務無法續期。
            logger.warning("GDrive auth_code 交換未取得 refresh_token（用戶可能已授權過）")
    else:
        # 新路徑（整頁 redirect）：用長期綁定的憑證換一份 access token
        cred = _get_drive_credential(db, current_user.id)
        if not cred:
            raise HTTPException(400, "尚未連接 Google Drive，請先完成授權")
        refresh_token = cred.refresh_token
        try:
            td = await _refresh_access_token(refresh_token)
        except TransientGoogleError:
            logger.warning("GDrive refresh 暫時無法處理（user_id=%s）", current_user.id, exc_info=True)
            raise HTTPException(503, "暫時無法連上 Google，請稍後再試")
        except RuntimeError:
            logger.warning("GDrive refresh token 失效（user_id=%s）", current_user.id, exc_info=True)
            raise HTTPException(409, "Google 授權已失效，請重新連接 Google Drive")
        except httpx.HTTPError:
            logger.warning("GDrive refresh 暫時無法連上 Google（user_id=%s）", current_user.id, exc_info=True)
            raise HTTPException(503, "暫時無法連上 Google，請稍後再試")
        access_token = td.get("access_token")
        expires_in = td.get("expires_in", 0)
        if not access_token:
            raise HTTPException(400, "Google 授權交換未取得 access token")

    # 續傳所需的完整參數：folder_ids / picked_files / AI 選項都只存在於這次請求裡，
    # 不落地的話重啟後就無法重建下載清單，續傳也就無從談起。
    params = {
        "folder_ids": folder_ids,
        "picked_files": picked_files,
        "fps": body.fps,
        "resolution": body.resolution,
        "rain_fog_detection": body.rain_fog_detection,
        "darkness_detection": body.darkness_detection,
        "image_recovery": body.image_recovery,
        "max_images": body.max_images,
        "duration_seconds": body.duration_seconds,
    }
    job = GDriveJob(
        user_id=current_user.id,
        folder_id=(folder_ids[0] if folder_ids else None),
        folder_name=body.selection_name or body.folder_name,
        google_refresh_token=refresh_token,
        status="pending", fps=body.fps, resolution=body.resolution,
        job_params=json.dumps(params, ensure_ascii=False),
    )
    db.add(job); db.commit(); db.refresh(job)
    _launch_gdrive_pipeline(
        job.id,
        folder_ids=folder_ids, picked_files=picked_files, refresh_token=refresh_token,
        body_fps=body.fps, body_resolution=body.resolution,
        rain_fog=body.rain_fog_detection, darkness=body.darkness_detection,
        max_images=body.max_images,
        initial_access_token=access_token, initial_expires_in=expires_in,
        duration_seconds=body.duration_seconds, image_recovery=body.image_recovery,
    )
    return {"job_id": job.id, "status": "pending", "message": "已開始：用你的 Google 權限下載照片 → Spark 生成"}


class GDriveResumeRequest(BaseModel):
    """續傳時可覆寫的設定（未給的沿用原本的 job_params）。

    只開放不會讓「已下載的照片作廢」的欄位：AI 選項與影片時長都只影響送 Spark
    之後的處理。resolution / fps 刻意不開放——照片是依當初的解析度抓對應尺寸的
    縮圖，事後改成 4K 只會拿到不足尺寸的素材。
    """
    rain_fog_detection: Optional[bool] = None
    darkness_detection: Optional[bool] = None
    image_recovery: Optional[bool] = None
    duration_seconds: Optional[int] = None


@router.post("/gdrive/{job_id}/resume")
async def resume_gdrive_job(
    job_id: int,
    body: Optional[GDriveResumeRequest] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """續傳一個被中斷的 Google Drive 任務，可順便改設定。

    NAS 上已下載的照片會全部保留並跳過，只補齊缺的部分；若上次已完成下載與抽樣，
    這裡等於只是重新送一次 Spark。需要 job_params（新版建立的任務才有）。

    可覆寫設定的用途：Spark 端因設定而失敗時（例如 job 42 的 quality gate 把
    整批照片刷光導致 worker 死鎖），沒有覆寫就只能原封重送、必然再失敗一次，
    否則得重新下載數 GB。
    """
    job = db.query(GDriveJob).filter(
        GDriveJob.id == job_id,
        GDriveJob.user_id == current_user.id,
    ).first()
    if not job:
        raise HTTPException(404, "找不到此任務")
    if job_id in _RUNNING_GDRIVE_TASKS:
        return {"job_id": job.id, "status": job.status, "message": "此任務正在執行中"}
    if (job.status or "") not in ("interrupted", "failed"):
        raise HTTPException(409, f"目前狀態（{job.status}）不需要續傳")
    if not job.job_params:
        raise HTTPException(
            409, "此任務是舊版建立的，沒有保存續傳所需的選取清單，請重新建立任務")
    try:
        params = json.loads(job.job_params)
    except Exception:
        raise HTTPException(500, "續傳參數毀損，請重新建立任務")

    refresh_token = job.google_refresh_token
    if not refresh_token:
        cred = _get_drive_credential(db, current_user.id)
        if not cred:
            raise HTTPException(400, "尚未連接 Google Drive，請先完成授權")
        refresh_token = cred.refresh_token
    try:
        td = await _refresh_access_token(refresh_token)
    except TransientGoogleError:
        raise HTTPException(503, "暫時無法連上 Google，請稍後再試")
    except RuntimeError:
        raise HTTPException(409, "Google 授權已失效，請重新連接 Google Drive")
    except httpx.HTTPError:
        raise HTTPException(503, "暫時無法連上 Google，請稍後再試")

    # 套用覆寫並寫回 job_params：之後若再被中斷，續傳要沿用新設定而不是退回舊的
    overrides = body.model_dump(exclude_none=True) if body else {}
    if overrides:
        params.update(overrides)
        job.job_params = json.dumps(params, ensure_ascii=False)
        logger.info("gdrive job %s 續傳並覆寫設定：%s", job_id, overrides)

    job.status = "pending"; job.error_message = None; db.commit()
    _launch_gdrive_pipeline(
        job.id,
        folder_ids=params.get("folder_ids") or [],
        picked_files=params.get("picked_files") or [],
        refresh_token=refresh_token,
        body_fps=params.get("fps") or job.fps or 30,
        body_resolution=params.get("resolution") or job.resolution,
        rain_fog=bool(params.get("rain_fog_detection")),
        darkness=bool(params.get("darkness_detection")),
        max_images=params.get("max_images"),
        initial_access_token=td.get("access_token"),
        initial_expires_in=td.get("expires_in", 0),
        duration_seconds=params.get("duration_seconds"),
        image_recovery=bool(params.get("image_recovery")),
    )
    msg = "已續傳：跳過已下載的照片，只補齊缺的部分"
    if overrides:
        msg += f"（已套用新設定：{', '.join(overrides)}）"
    return {"job_id": job.id, "status": "pending", "message": msg, "applied_overrides": overrides}



@router.get("/gdrive")
async def list_gdrive_jobs(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """列出目前用戶的所有 Google Drive 縮時 jobs"""
    jobs = db.query(GDriveJob).filter(
        GDriveJob.user_id == current_user.id
    ).order_by(GDriveJob.created_at.desc()).all()
    return [
        {
            "job_id": job.id,
            "status": job.status,
            "folder_url": job.folder_url,
            "folder_name": job.folder_name,
            "fps": job.fps,
            "resolution": job.resolution,
            "total_images": job.total_images,
            "downloaded_count": job.downloaded_count,
            "video_download_url": _video_download_url(job),
            "error_message": job.error_message,
            "resumable": job.status in ("interrupted", "failed") and bool(job.job_params),
            "created_at": utc_iso(job.created_at),
        }
        for job in jobs
    ]

@router.get("/gdrive/{job_id}")
async def get_gdrive_job(
    job_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """查詢 Google Drive 縮時 job 進度。

    送 Spark 前（pending/listing/downloading/submitted）回本地 GDriveJob 進度；
    一旦有 spark_job_id 就直接輪詢 Spark GET /jobs/{id} 取即時狀態（不再 proxy Camera Backend）。
    """
    job = db.query(GDriveJob).filter(
        GDriveJob.id == job_id,
        GDriveJob.user_id == current_user.id,
    ).first()
    if not job:
        raise HTTPException(404, "找不到此任務")

    total = job.total_images or 0
    downloaded = job.downloaded_count or 0

    # 階段一：尚未送 Spark → 回本地下載進度
    if not job.spark_job_id:
        local_percent = {"pending": 2, "listing": 5, "downloading": 0, "submitted": 60,
                         "failed": 0, "interrupted": 0}
        percent = local_percent.get(job.status, 2)
        if job.status in ("downloading", "interrupted") and total > 0:
            percent = max(5, int(downloaded / total * 55))  # 下載階段佔 5~60%
        stage_map = {
            "pending": "準備中",
            "listing": "讀取 Google Drive 資料夾中",
            "downloading": f"下載照片中（{downloaded}/{total}）",
            "submitted": "送出 Spark 生成中",
            "failed": "失敗",
            "interrupted": f"已中斷（{downloaded}/{total}），可續傳",
        }
        return {
            "job_id": job.id,
            "status": job.status,
            "percent_complete": percent,
            "image_count": total,
            "downloaded_count": downloaded,
            "current_stage": stage_map.get(job.status, "處理中"),
            "video_download_url": _video_download_url(job),
            "error_message": job.error_message,
            # 前端據此顯示「繼續下載」；舊任務沒有 job_params 無法續傳
            "resumable": job.status in ("interrupted", "failed") and bool(job.job_params),
        }

    # 階段二：已送 Spark → 直接問 Spark
    spark = {}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            sr = await client.get(
                f"{SPARK_API_URL}/jobs/{job.spark_job_id}",
                headers={"x-api-key": SPARK_API_KEY},
            )
        if sr.status_code == 200:
            spark = sr.json()
    except Exception:
        pass

    sp = (spark.get("status") or job.status or "").lower()
    if sp in ("completed", "done", "success"):
        status = "completed"
    elif sp in ("failed", "error"):
        status = "failed"
    else:
        status = "processing"

    percent = spark.get("percent_complete")
    if percent is None:
        percent = 100 if status == "completed" else (0 if status == "failed" else 70)
    image_count = spark.get("image_count") or total
    error = spark.get("error") or job.error_message

    video_url = _video_download_url(job) if status == "completed" else None

    # 同步 DB 終態
    if status in ("completed", "failed") and job.status != status:
        job.status = status
        if error:
            job.error_message = error
        db.commit()

    # current_stage 是要顯示給使用者的中文標籤，所以用我們自己的——Spark 的
    # current_stage 是它的內部英文狀態（實測就是 "processing"），採用它會讓中文
    # 介面冒出英文字，而且資訊量還不如我們自己的標籤。
    stage_label = {"completed": "完成", "failed": "失敗"}.get(status, "生成中")
    # Spark 的細節與 ETA 才是使用者真正想知道的（"Batch 7/50 ...", "3h 27m"），
    # 原本整個被丟掉，畫面上只剩一個百分比，看起來就像卡住。
    return {
        "job_id": job.id,
        "status": status,
        "percent_complete": percent,
        "image_count": image_count,
        "downloaded_count": downloaded,
        "current_stage": stage_label,
        "stage_detail": spark.get("stage_detail") or None,
        "estimated_time_remaining": spark.get("estimated_time_remaining") or None,
        "spark_status": spark.get("status") or None,
        # 診斷用：spark_job_id 原本只存在 DB，任何 API 都不吐，導致「任務看似卡住」時
        # 沒有任何辦法從外部直接去問 Spark。spark_stage 是 Spark 自己的階段字串（被上面
        # 的中文標籤蓋掉了），quality_report 則原本整個被丟棄——品質關卡重跑時只有它看得出來。
        "spark_job_id": job.spark_job_id or None,
        "spark_stage": spark.get("current_stage") or None,
        "quality_report": spark.get("quality_report") or None,
        "video_download_url": video_url,
        "error_message": error,
    }


class SparkCallback(BaseModel):
    status: Optional[str] = None
    error: Optional[str] = None
    error_message: Optional[str] = None
    video_url: Optional[str] = None
    percent_complete: Optional[int] = None


@router.post("/gdrive/callback/{job_id}")
async def gdrive_spark_callback(
    job_id: int,
    body: SparkCallback,
    request: Request,
    db: Session = Depends(get_db),
):
    """Spark 完成後的 server-to-server 回呼（不需 user token）。
    狀態主要仍由 get_gdrive_job 輪詢取得，此回呼為即時補強。
    若有帶 x-api-key 則須與 SPARK_API_KEY 相符（沒帶則放行，因 Spark 端帶法未定）。
    """
    key = request.headers.get("x-api-key")
    if key and key != SPARK_API_KEY:
        raise HTTPException(403, "Invalid api key")

    job = db.query(GDriveJob).filter(GDriveJob.id == job_id).first()
    if not job:
        return {"message": "job not found, ignored"}

    if body.status:
        job.status = body.status
    err = body.error or body.error_message
    if err:
        job.error_message = err
    if body.status == "completed":
        if job.spark_job_id:
            job.video_url = None      # 連結改為查詢時即時產生（見 _video_download_url）
        elif body.video_url:
            job.video_url = body.video_url
    db.commit()
    return {"message": "ok"}


@router.delete("/gdrive/{job_id}")
async def delete_gdrive_job(
    job_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """刪除 Google Drive 縮時 job"""
    job = db.query(GDriveJob).filter(
        GDriveJob.id == job_id,
        GDriveJob.user_id == current_user.id,
    ).first()
    if not job:
        raise HTTPException(404, "找不到此任務")

    # 如果 Spark 有 job，也刪掉
    if job.spark_job_id:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.delete(
                    f"{SPARK_API_URL}/jobs/{job.spark_job_id}",
                    headers={"x-api-key": SPARK_API_KEY},
                )
        except Exception:
            pass

    db.delete(job)
    db.commit()
    return {"message": "已刪除"}
