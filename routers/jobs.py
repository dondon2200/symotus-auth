from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from datetime import datetime
from typing import Optional
from pydantic import BaseModel

from database import get_db
from models import User, TimelapsJob
from auth import get_current_user

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
    created_at: datetime
    updated_at: datetime

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
def list_jobs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return db.query(TimelapsJob).filter(
        TimelapsJob.user_id == current_user.id
    ).order_by(TimelapsJob.created_at.desc()).all()


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

    if body.status is not None: job.status = body.status
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
import time
import logging
import httpx
from models import GDriveJob
from config import settings

logger = logging.getLogger(__name__)

SPARK_API_URL = settings.SPARK_API_URL
SPARK_API_KEY = settings.SPARK_API_KEY
GDRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
DOWNLOAD_CONCURRENCY = 12  # Drive API alt=media 並發路數（§3：8~16）


# ── OAuth：用消費者授權碼換 token、refresh token 續期 ──────────────────────────

async def _exchange_auth_code(auth_code: str) -> dict:
    """用前端 GIS code client（ux_mode=popup）拿到的授權碼換 access + refresh token。

    重點：popup 模式的授權碼必須以 redirect_uri='postmessage' 交換（GIS 慣例），
    不是 web OAuth 的 GOOGLE_REDIRECT_URI。
    """
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(GOOGLE_TOKEN_URL, data={
            "code": auth_code,
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "redirect_uri": "postmessage",
            "grant_type": "authorization_code",
        })
    if r.status_code != 200:
        raise HTTPException(400, f"Google 授權碼交換失敗（{r.status_code}）：{r.text[:200]}")
    return r.json()


async def _refresh_access_token(refresh_token: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(GOOGLE_TOKEN_URL, data={
            "refresh_token": refresh_token,
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "grant_type": "refresh_token",
        })
    if r.status_code != 200:
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


async def _list_drive_images(token_mgr: "_TokenManager", folder_id: str, max_images: Optional[int] = None) -> list[dict]:
    """用消費者授權列出資料夾內的圖片（drive.file scope，Picker 選到的資料夾可列舉子項）。"""
    files: list[dict] = []
    page_token = None
    limit = max_images or 100000

    async with httpx.AsyncClient(timeout=30) as client:
        while len(files) < limit:
            access = await token_mgr.get()
            params = {
                "q": f"'{folder_id}' in parents and mimeType contains 'image/' and trashed=false",
                "fields": "nextPageToken,files(id,name,mimeType,size)",
                "pageSize": 1000,
                "orderBy": "name",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
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
            files.extend(data.get("files", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break

    return files[:limit]


def _write_file(path: str, data: bytes):
    with open(path, "wb") as fp:
        fp.write(data)


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




async def _run_gdrive_nas_pipeline(job_id: int, folder_ids: list[str], picked_files: list[dict],
                                   refresh_token: Optional[str],
                                   body_fps: int, body_resolution, rain_fog: bool, darkness: bool,
                                   max_images=None, initial_access_token: Optional[str] = None,
                                   initial_expires_in: int = 0):
    """背景任務：用消費者自己的 Drive 授權並發下載 → NAS → Spark /jobs/nas。

    下載清單 = 個別選取的照片（picked_files）+ 各選取資料夾列出的照片（依 id 去重）。
    走 Drive API v3 files.get?alt=media（per-user 配額、不限速、不跳病毒掃描頁），並發
    DOWNLOAD_CONCURRENCY 路；token 到期用 refresh token 自動續期。NAS→Spark 段沿用。
    """
    from database import SessionLocal
    db = SessionLocal()
    nas_folder = f"gdrive_{job_id}"
    nas_path = f"/homes/firmness/{nas_folder}"
    token_mgr = _TokenManager(refresh_token, initial_access_token, initial_expires_in)
    try:
        import shutil
        job = db.query(GDriveJob).filter(GDriveJob.id == job_id).first()
        if not job: return

        if os.path.exists(nas_path):
            shutil.rmtree(nas_path)
        os.makedirs(nas_path, exist_ok=True)

        # 1. 建下載清單：個別照片 + 各資料夾列出的照片（依 id 去重）
        job.status = "listing"; db.commit()
        download: list[dict] = []
        seen: set[str] = set()
        for it in picked_files:
            if it.get("id") and it["id"] not in seen:
                seen.add(it["id"]); download.append({"id": it["id"], "name": it.get("name") or it["id"]})
        try:
            for folder_id in folder_ids:
                for f in await _list_drive_images(token_mgr, folder_id, max_images):
                    if f["id"] not in seen:
                        seen.add(f["id"]); download.append({"id": f["id"], "name": f["name"]})
        except Exception as e:
            job.status = "failed"; job.error_message = f"無法讀取資料夾：{e}"; db.commit()
            shutil.rmtree(nas_path, ignore_errors=True); return
        if not download:
            job.status = "failed"; job.error_message = "選取的項目中沒有找到圖片"; db.commit()
            shutil.rmtree(nas_path, ignore_errors=True); return
        job.total_images = len(download); db.commit()

        # 2. 並發下載到 NAS（Drive API alt=media）。檔名加序號前綴：保序＋避免同名覆蓋。
        job.status = "downloading"; db.commit()
        sem = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)
        progress = {"done": 0}
        progress_lock = asyncio.Lock()
        async with httpx.AsyncClient(timeout=120) as session:
            async def fetch_one(idx: int, item: dict):
                async with sem:
                    data = await _download_file_api(session, token_mgr, item["id"])
                if data:
                    safe = str(item["name"]).replace("/", "_").replace("\\", "_")
                    await asyncio.to_thread(_write_file, os.path.join(nas_path, f"{idx:06d}_{safe}"), data)
                async with progress_lock:
                    progress["done"] += 1
                    if progress["done"] % 25 == 0:
                        job.downloaded_count = progress["done"]; db.commit()
            await asyncio.gather(*[fetch_one(i, it) for i, it in enumerate(download)])

        saved = len([x for x in os.listdir(nas_path) if not x.startswith(".")])
        job.downloaded_count = saved; db.commit()
        if saved < 10:
            job.status = "failed"; job.error_message = f"只下載到 {saved} 張"; db.commit()
            shutil.rmtree(nas_path, ignore_errors=True); return

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
            job.status = "failed"; job.error_message = f"Spark 錯誤（{sr.status_code}）：{sr.text[:200]}"; db.commit()
            shutil.rmtree(nas_path, ignore_errors=True); return
        job.spark_job_id = str(sr.json().get("job_id", ""))
        job.status = "processing"; db.commit()
    except Exception as e:
        try:
            job = db.query(GDriveJob).filter(GDriveJob.id == job_id).first()
            if job: job.status = "failed"; job.error_message = str(e)[:300]; db.commit()
        except Exception: pass
    finally:
        db.close()


class FileRef(BaseModel):
    id: str
    name: Optional[str] = None


class GDriveJobRequest(BaseModel):
    folder_id: Optional[str] = None          # 向後相容：單一資料夾
    folder_name: Optional[str] = None
    folder_ids: Optional[list[str]] = None   # 多選：資料夾（後端列出內含照片）
    files: Optional[list[FileRef]] = None    # 多選：個別照片
    selection_name: Optional[str] = None     # 顯示用（如「2 個資料夾、30 張照片」）
    auth_code: str
    fps: int = 30
    resolution: Optional[str] = "1920x1080"
    rain_fog_detection: bool = False
    darkness_detection: bool = False
    image_recovery: bool = False
    stabilization: bool = False
    max_images: Optional[int] = None


@router.post("/gdrive")
async def create_gdrive_job(
    body: GDriveJobRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """從 Google Drive 建立縮時影片 job（OAuth + Picker 流程，支援多選）。

    消費者用 GIS code client 取得 offline 授權碼（auth_code），後端在此換 refresh + access
    token，再用消費者自己的權限並發下載 Picker 選到的「照片與/或資料夾」→ NAS
    （/homes/firmness/gdrive_<id>）→ 呼叫 Spark POST /jobs/nas。不再爬公開連結。
    """
    folder_ids = list(body.folder_ids or [])
    if body.folder_id:
        folder_ids.append(body.folder_id)
    picked_files = [{"id": f.id, "name": f.name} for f in (body.files or []) if f.id]
    if not folder_ids and not picked_files:
        raise HTTPException(400, "未選取任何資料夾或照片")
    if not body.auth_code:
        raise HTTPException(400, "缺少 Google 授權碼")
    if not (settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET):
        raise HTTPException(500, "伺服器未設定 Google OAuth 憑證（GOOGLE_CLIENT_ID/SECRET）")

    # 用授權碼換 token（offline）。popup 模式以 redirect_uri='postmessage' 交換。
    td = await _exchange_auth_code(body.auth_code)
    access_token = td.get("access_token")
    refresh_token = td.get("refresh_token")
    if not access_token:
        raise HTTPException(400, "Google 授權交換未取得 access token")
    if not refresh_token:
        # 用戶若先前已授權，Google 可能不再回 refresh token；短任務用 access token 仍可完成，
        # 但超過 token 壽命的長任務無法續期。
        logger.warning("GDrive auth_code 交換未取得 refresh_token（用戶可能已授權過）")

    job = GDriveJob(
        user_id=current_user.id,
        folder_id=(folder_ids[0] if folder_ids else None),
        folder_name=body.selection_name or body.folder_name,
        google_refresh_token=refresh_token,
        status="pending", fps=body.fps, resolution=body.resolution,
    )
    db.add(job); db.commit(); db.refresh(job)
    asyncio.create_task(_run_gdrive_nas_pipeline(
        job.id, folder_ids, picked_files, refresh_token, body.fps, body.resolution,
        body.rain_fog_detection, body.darkness_detection, body.max_images,
        access_token, td.get("expires_in", 0),
    ))
    return {"job_id": job.id, "status": "pending", "message": "已開始：用你的 Google 權限下載照片 → Spark 生成"}



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
            "video_download_url": job.video_url,
            "error_message": job.error_message,
            "created_at": job.created_at.isoformat() if job.created_at else None,
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
        local_percent = {"pending": 2, "listing": 5, "downloading": 0, "submitted": 60, "failed": 0}
        percent = local_percent.get(job.status, 2)
        if job.status == "downloading" and total > 0:
            percent = max(5, int(downloaded / total * 55))  # 下載階段佔 5~60%
        stage_map = {
            "pending": "準備中",
            "listing": "讀取 Google Drive 資料夾中",
            "downloading": f"下載照片中（{downloaded}/{total}）",
            "submitted": "送出 Spark 生成中",
            "failed": "失敗",
        }
        return {
            "job_id": job.id,
            "status": job.status,
            "percent_complete": percent,
            "image_count": total,
            "downloaded_count": downloaded,
            "current_stage": stage_map.get(job.status, "處理中"),
            "video_download_url": job.video_url,
            "error_message": job.error_message,
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

    video_url = job.video_url
    if status == "completed" and not video_url:
        video_url = f"{SPARK_API_URL}/jobs/{job.spark_job_id}/download?api_key={SPARK_API_KEY}"

    # 同步 DB 終態
    if status in ("completed", "failed") and job.status != status:
        job.status = status
        if video_url:
            job.video_url = video_url
        if error:
            job.error_message = error
        db.commit()

    return {
        "job_id": job.id,
        "status": status,
        "percent_complete": percent,
        "image_count": image_count,
        "downloaded_count": downloaded,
        "current_stage": spark.get("current_stage") or ("生成中" if status == "processing" else status),
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
            job.video_url = f"{SPARK_API_URL}/jobs/{job.spark_job_id}/download?api_key={SPARK_API_KEY}"
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
