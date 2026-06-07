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

import re
import asyncio
import os
import httpx
from models import GDriveJob
from config import settings

SPARK_API_URL = settings.SPARK_API_URL
SPARK_API_KEY = settings.SPARK_API_KEY
GDRIVE_API_KEY = "AIzaSyAj-LJs2lbT7pjh0FFkOw-rRH-OMtMl3c4"
GDRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
GDRIVE_DOWNLOAD_URL = "https://drive.google.com/uc"  # 不走 API，直接 HTTP 下載


def _extract_folder_id(folder_url: str) -> Optional[str]:
    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", folder_url)
    return m.group(1) if m else None


async def _list_drive_images(folder_id: str, max_images: Optional[int] = None) -> list[dict]:
    """用 Google Drive API 列出資料夾內的圖片（只列清單，不下載）"""
    files = []
    page_token = None
    limit = max_images or 9999

    async with httpx.AsyncClient(timeout=30) as client:
        while len(files) < limit:
            params = {
                "q": f"'{folder_id}' in parents and trashed=false",
                "fields": "nextPageToken,files(id,name,mimeType,size)",
                "pageSize": min(1000, limit - len(files)),
                "key": GDRIVE_API_KEY,
                "orderBy": "name",
            }
            if page_token:
                params["pageToken"] = page_token

            resp = await client.get(GDRIVE_FILES_URL, params=params)
            if resp.status_code != 200:
                raise HTTPException(400, f"無法列出資料夾內容（{resp.status_code}）")

            data = resp.json()
            image_files = [
                f for f in data.get("files", [])
                if f.get("mimeType", "").startswith("image/")
            ]
            files.extend(image_files)
            page_token = data.get("nextPageToken")
            if not page_token:
                break

    return files[:limit]


async def _download_file_direct(session: httpx.AsyncClient, file_id: str) -> Optional[bytes]:
    """用公開 URL 下載 Google Drive 單張圖片（不需 API key，支援公開分享資料夾）"""
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    for attempt in range(3):  # 最多 3 次 retry
        try:
            resp = await session.get(url, follow_redirects=True, timeout=45)
            if resp.status_code == 200 and len(resp.content) > 1000:
                return resp.content
            if resp.status_code in (403, 429) and attempt < 2:
                await asyncio.sleep(2 ** attempt)  # 1s, 2s
                continue
            return None
        except Exception:
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
    return None




async def _run_gdrive_nas_pipeline(job_id: int, folder_id: str, body_fps: int, body_resolution, max_images=None):
    """背景任務：Google Drive → NAS（用 gdown）→ Spark /jobs/nas（無張數限制、無 API key）"""
    from database import SessionLocal
    db = SessionLocal()
    nas_folder = f"gdrive_{job_id}"
    nas_path = f"/homes/firmness/{nas_folder}"
    try:
        import shutil, concurrent.futures
        job = db.query(GDriveJob).filter(GDriveJob.id == job_id).first()
        if not job: return

        job.status = "downloading"; db.commit()

        # 建立 NAS 目標資料夾
        if os.path.exists(nas_path):
            shutil.rmtree(nas_path)
        os.makedirs(nas_path, exist_ok=True)

        # 用 gdown 下載整個資料夾（不需 API key，支援公開分享資料夾）
        folder_url = f"https://drive.google.com/drive/folders/{folder_id}"
        loop = asyncio.get_event_loop()

        def run_gdown():
            import gdown, glob
            try:
                gdown.download_folder(
                    url=folder_url,
                    output=nas_path,
                    quiet=False,
                    use_cookies=False,
                    remaining_ok=True,
                )
                # gdown 會在 nas_path 下建子資料夾，把檔案移到 nas_path 根目錄
                subdirs = [d for d in os.listdir(nas_path)
                          if os.path.isdir(os.path.join(nas_path, d))]
                for subdir in subdirs:
                    subdir_path = os.path.join(nas_path, subdir)
                    for fname in os.listdir(subdir_path):
                        src = os.path.join(subdir_path, fname)
                        dst = os.path.join(nas_path, fname)
                        if not os.path.exists(dst):
                            shutil.move(src, dst)
                    shutil.rmtree(subdir_path, ignore_errors=True)
                return None
            except Exception as e:
                return str(e)

        # 在 executor 跑同步的 gdown（避免 blocking event loop）
        error = await loop.run_in_executor(None, run_gdown)
        if error:
            job.status = "failed"; job.error_message = f"Google Drive 下載失敗：{error}"; db.commit()
            shutil.rmtree(nas_path, ignore_errors=True); return

        # 計算下載了幾張（支援子資料夾遞迴）
        image_exts = {".jpg",".jpeg",".png",".bmp",".tif",".tiff"}
        downloaded_files = [f for f in os.listdir(nas_path)
                           if os.path.splitext(f)[1].lower() in image_exts]
        downloaded = len(downloaded_files)
        job.downloaded_count = downloaded
        job.total_images = downloaded
        db.commit()

        if downloaded < 10:
            job.status = "failed"
            job.error_message = f"只下載到 {downloaded} 張，請確認：1) 資料夾已公開分享 2) 資料夾內有圖片"
            db.commit()
            shutil.rmtree(nas_path, ignore_errors=True); return

        # 呼叫 Spark /jobs/nas
        job.status = "submitted"; db.commit()
        callback_url = f"{settings.PUBLIC_BASE_URL}/jobs/gdrive/callback/{job_id}"
        async with httpx.AsyncClient(timeout=30) as client:
            spark_resp = await client.post(
                f"{SPARK_API_URL}/jobs/nas",
                headers={"x-api-key": SPARK_API_KEY},
                json={
                    "nas_path": nas_folder,
                    "callback_url": callback_url,
                    "fps": body_fps,
                    "resolution": body_resolution,
                    "rain_fog_detection": False,
                    "darkness_detection": False,
                },
            )
        if spark_resp.status_code not in (200, 202):
            job.status = "failed"
            job.error_message = f"Spark 錯誤（{spark_resp.status_code}）：{spark_resp.text[:200]}"
            db.commit()
            shutil.rmtree(nas_path, ignore_errors=True); return

        spark_data = spark_resp.json()
        job.spark_job_id = str(spark_data.get("job_id", ""))
        job.status = "processing"; db.commit()

    except Exception as e:
        try:
            job = db.query(GDriveJob).filter(GDriveJob.id == job_id).first()
            if job: job.status = "failed"; job.error_message = str(e)[:300]; db.commit()
        except Exception: pass
    finally:
        db.close()

async def _run_gdrive_background_full(job_id: int, folder_id: str, body_fps: int, body_resolution: Optional[str], max_images: Optional[int] = None):
    """背景任務：列清單 + 分批下載照片 → 送 Spark"""
    from database import SessionLocal
    db = SessionLocal()
    try:
        job = db.query(GDriveJob).filter(GDriveJob.id == job_id).first()
        if not job:
            return

        # 列出檔案清單
        MAX_IMAGES = 1000
        max_req = min(max_images or MAX_IMAGES, MAX_IMAGES)
        try:
            files = await _list_drive_images(folder_id, max_req)
        except Exception as e:
            job.status = "failed"
            job.error_message = f"無法讀取資料夾：{e}"
            db.commit()
            return

        if not files:
            job.status = "failed"
            job.error_message = "資料夾內沒有找到圖片"
            db.commit()
            return

        if len(files) < 100:
            job.status = "failed"
            job.error_message = f"圖片數量不足（{len(files)} 張），縮時影片至少需要 100 張"
            db.commit()
            return

        job.total_images = len(files)
        if len(files) >= MAX_IMAGES:
            job.error_message = f"資料夾內超過 {MAX_IMAGES} 張照片，將只處理前 {MAX_IMAGES} 張"
        db.commit()

        await _run_gdrive_background(job_id, files, body_fps, body_resolution, _db=db)
    except Exception as e:
        try:
            job = db.query(GDriveJob).filter(GDriveJob.id == job_id).first()
            if job:
                job.status = "failed"
                job.error_message = str(e)[:300]
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


async def _run_gdrive_background(job_id: int, files: list[dict], body_fps: int, body_resolution: Optional[str], _db=None):
    """背景任務：下載照片到磁碟 → 送 Spark（避免記憶體爆炸）"""
    import tempfile, shutil
    from database import SessionLocal
    db = _db or SessionLocal()
    own_db = _db is None
    tmp_dir = None
    try:
        job = db.query(GDriveJob).filter(GDriveJob.id == job_id).first()
        if not job:
            return

        # 建暫存目錄
        tmp_dir = tempfile.mkdtemp(prefix=f"gdrive_{job_id}_")

        # 下載階段：寫磁碟，不存記憶體
        job.status = "downloading"
        db.commit()

        saved_files = []
        async with httpx.AsyncClient(timeout=60) as session:
            for i, f in enumerate(files):
                img_bytes = await _download_file_direct(session, f["id"])
                if img_bytes:
                    fpath = os.path.join(tmp_dir, f["name"])
                    with open(fpath, "wb") as fp:
                        fp.write(img_bytes)
                    saved_files.append((f["name"], fpath))
                    del img_bytes  # 立刻釋放記憶體
                job.downloaded_count = i + 1
                if (i + 1) % 10 == 0:
                    db.commit()
                if (i + 1) % 50 == 0:
                    await asyncio.sleep(0.5)

        job.downloaded_count = len(saved_files)
        db.commit()

        if len(saved_files) < 100:
            job.status = "failed"
            job.error_message = f"成功下載 {len(saved_files)} 張，不足 100 張無法生成縮時影片"
            db.commit()
            return

        # 上傳 Spark 階段：採樣避免 413（Spark 有 payload 限制）
        job.status = "uploading"
        db.commit()

        callback_url = f"{settings.PUBLIC_BASE_URL}/jobs/gdrive/callback/{job_id}"

        # 若照片超過 300 張，均勻採樣減少到 300（Spark 有大小限制）
        MAX_SPARK_FILES = 300
        if len(saved_files) > MAX_SPARK_FILES:
            step = len(saved_files) / MAX_SPARK_FILES
            sampled = [saved_files[int(i * step)] for i in range(MAX_SPARK_FILES)]
            logger.info(f"GDrive job {job_id}: sampled {len(saved_files)} → {len(sampled)} files for Spark")
        else:
            sampled = saved_files

        file_handles = []
        form_files = []
        try:
            for name, fpath in sampled:
                fh = open(fpath, "rb")
                file_handles.append(fh)
                form_files.append(("images", (name, fh, "image/jpeg")))

            async with httpx.AsyncClient(timeout=600) as client:
                spark_resp = await client.post(
                    f"{SPARK_API_URL}/jobs",
                    headers={"x-api-key": SPARK_API_KEY},
                    data={"callback_url": callback_url, "fps": str(body_fps)},
                    files=form_files,
                )
        finally:
            for fh in file_handles:
                fh.close()

        if spark_resp.status_code not in [200, 202]:
            job.status = "failed"
            job.error_message = f"Spark 服務錯誤（{spark_resp.status_code}）"
            db.commit()
            return

        spark_data = spark_resp.json()
        job.spark_job_id = str(spark_data.get("job_id", ""))
        job.status = "processing"
        db.commit()

    except Exception as e:
        try:
            job = db.query(GDriveJob).filter(GDriveJob.id == job_id).first()
            if job:
                job.status = "failed"
                job.error_message = str(e)[:300]
                db.commit()
        except Exception:
            pass
    finally:
        if tmp_dir and os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)
        if own_db:
            db.close()


class GDriveJobRequest(BaseModel):
    folder_url: str
    fps: int = 30
    resolution: Optional[str] = "1920x1080"
    max_images: Optional[int] = None


@router.post("/gdrive")
async def create_gdrive_job(
    body: GDriveJobRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """從 Google Drive 公開資料夾建立縮時影片 job（委派給 Camera Backend）"""
    CAMERA_BACKEND = "https://user.symotus.com"
    CAM_SVCKEY = os.environ.get("CAMERA_SERVICE_KEY", "")
    async with httpx.AsyncClient(timeout=15) as client:
        tok_resp = await client.post(
            f"{CAMERA_BACKEND}/internal/auth/token",
            headers={"x-service-key": CAM_SVCKEY},
            json={"user_id": 0, "email": "admin@timelapse.com", "role": "symotus_admin"},
        )
    if tok_resp.status_code != 200:
        raise HTTPException(502, f"無法取得 Camera Backend token")
    cam_token = tok_resp.json().get("access_token", "")

    # 呼叫 Camera Backend gdrive-import API（下載到 NAS → Spark）
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{CAMERA_BACKEND}/api/timelapse/gdrive-import",
            headers={"Authorization": f"Bearer {cam_token}"},
            json={
                "folder_url": body.folder_url,
                "fps": body.fps,
                "resolution": body.resolution,
                "max_images": body.max_images,  # None = Camera Backend 決定上限
            },
        )
    if resp.status_code not in (200, 201):
        # Camera Backend 失敗 → fallback 到 auth service 自己下載
        import re
        match = re.search(r"/folders/([a-zA-Z0-9_-]+)", body.folder_url)
        if not match:
            raise HTTPException(400, "無效的 Google Drive 資料夾連結")
        folder_id = match.group(1)
        job = GDriveJob(
            user_id=current_user.id, folder_url=body.folder_url,
            status="pending", fps=body.fps, resolution=body.resolution,
        )
        db.add(job); db.commit(); db.refresh(job)
        asyncio.create_task(_run_gdrive_background_full(
            job.id, folder_id, body.fps, body.resolution, body.max_images
        ))
        return {"job_id": job.id, "status": "pending", "message": "Camera Backend 不可用，使用備用下載"}

    cam_data = resp.json()
    cam_job_id = cam_data.get("job_id")
    job = GDriveJob(
        user_id=current_user.id, folder_url=body.folder_url,
        status="pending", spark_job_id=str(cam_job_id), fps=body.fps, resolution=body.resolution,
    )
    db.add(job); db.commit(); db.refresh(job)
    return {"job_id": job.id, "status": "pending", "message": "Camera Backend 正在處理（下載 NAS → Spark）"}



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
    """查詢 Google Drive 縮時 job 進度（proxy Camera Backend）"""
    job = db.query(GDriveJob).filter(
        GDriveJob.id == job_id,
        GDriveJob.user_id == current_user.id,
    ).first()
    if not job:
        raise HTTPException(404, "找不到此任務")

    if not job.spark_job_id:
        return {"job_id": job.id, "status": "pending", "percent_complete": 0, "current_stage": "準備中"}

    # Proxy Camera Backend 取即時狀態
    CAMERA_BACKEND = "https://user.symotus.com"
    CAM_SVCKEY = os.environ.get("CAMERA_SERVICE_KEY", "")
    cam_status = {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            tok_resp = await client.post(
                f"{CAMERA_BACKEND}/internal/auth/token",
                headers={"x-service-key": CAM_SVCKEY},
                json={"user_id": 0, "email": "admin@timelapse.com", "role": "symotus_admin"},
            )
        cam_token = tok_resp.json().get("access_token", "") if tok_resp.status_code == 200 else ""
        async with httpx.AsyncClient(timeout=15) as client:
            cam_resp = await client.get(
                f"{CAMERA_BACKEND}/api/timelapse/jobs/{job.spark_job_id}",
                headers={"Authorization": f"Bearer {cam_token}"},
            )
        if cam_resp.status_code == 200:
            cam_status = cam_resp.json()
    except Exception:
        pass

    status = cam_status.get("status", job.status)
    image_count = cam_status.get("image_count", 0)
    downloaded = cam_status.get("downloaded_count", 0)
    error = cam_status.get("error_message") or cam_status.get("spark_error")
    video_url = cam_status.get("video_url")

    # Camera Backend status → percent
    percent_map = {"pending": 2, "listing": 5, "downloading": 0, "submitted": 70, "completed": 100, "failed": 0}
    percent = percent_map.get(status, 2)
    if status == "downloading" and image_count > 0:
        percent = max(5, int(downloaded / image_count * 60))

    stage_map = {
        "pending": "準備中",
        "listing": "讀取 Google Drive 資料夾中",
        "downloading": f"下載照片中（{downloaded}/{image_count}）",
        "submitted": "送出 Spark 生成中",
        "completed": "完成",
        "failed": "失敗",
    }

    # 同步 DB
    if status in ("completed", "failed") and job.status != status:
        job.status = status
        if video_url: job.video_url = video_url
        if error: job.error_message = error
        db.commit()

    return {
        "job_id": job.id,
        "status": status,
        "percent_complete": percent,
        "image_count": image_count,
        "downloaded_count": downloaded,
        "current_stage": stage_map.get(status, "處理中"),
        "video_download_url": video_url or job.video_url,
        "error_message": error or job.error_message,
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
