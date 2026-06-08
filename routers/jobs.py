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
import logging
import httpx
from models import GDriveJob
from config import settings

logger = logging.getLogger(__name__)

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
    """用公開 URL 下載 Google Drive 單張圖片（不需 API key，支援公開分享資料夾）。

    重點：必須檢查 Content-Type 是否為 image/*，因為 Google 對大檔/異常會回傳
    text/html 的「病毒掃描確認頁」或登入頁（長度可能 >1000），只看 length 會把 HTML
    當成壞圖存下。遇到 HTML 確認頁則解析 confirm token 再下載一次。
    """
    base = "https://drive.google.com/uc?export=download"
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}
    url = f"{base}&id={file_id}"
    for attempt in range(3):  # 最多 3 次 retry
        try:
            resp = await session.get(url, headers=headers, follow_redirects=True, timeout=60)
            ctype = resp.headers.get("content-type", "").lower()
            if resp.status_code == 200 and ctype.startswith("image/") and len(resp.content) > 1000:
                return resp.content
            # 大檔會回 HTML 確認頁，解析 confirm token 後再抓一次
            if resp.status_code == 200 and "text/html" in ctype:
                m = re.search(r"confirm=([0-9A-Za-z_\-]+)", resp.text)
                if m:
                    confirm_url = f"{base}&confirm={m.group(1)}&id={file_id}"
                    r2 = await session.get(confirm_url, headers=headers, follow_redirects=True, timeout=120)
                    if (r2.status_code == 200
                            and r2.headers.get("content-type", "").lower().startswith("image/")
                            and len(r2.content) > 1000):
                        return r2.content
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return None
            if resp.status_code in (403, 429) and attempt < 2:
                await asyncio.sleep(2 ** attempt)  # 1s, 2s
                continue
            return None
        except Exception:
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
    return None




async def _run_gdrive_nas_pipeline(job_id: int, folder_id: str, body_fps: int, body_resolution, max_images=None):
    """背景任務：Google Drive → NAS（原本有效下載方式）→ Spark /jobs/nas"""
    from database import SessionLocal
    db = SessionLocal()
    nas_folder = f"gdrive_{job_id}"
    nas_path = f"/homes/firmness/{nas_folder}"
    try:
        import shutil
        job = db.query(GDriveJob).filter(GDriveJob.id == job_id).first()
        if not job: return

        if os.path.exists(nas_path):
            shutil.rmtree(nas_path)
        os.makedirs(nas_path, exist_ok=True)

        # 1. 列清單（原本就能跑的方式）
        job.status = "listing"; db.commit()
        try:
            files = await _list_drive_images(folder_id, max_images)
        except Exception as e:
            job.status = "failed"; job.error_message = f"無法讀取資料夾：{e}"; db.commit()
            shutil.rmtree(nas_path, ignore_errors=True); return
        if not files:
            job.status = "failed"; job.error_message = "資料夾內沒有找到圖片"; db.commit()
            shutil.rmtree(nas_path, ignore_errors=True); return
        job.total_images = len(files); db.commit()

        # 2. 下載到 NAS（原本就能跑，用公開 URL drive.google.com/uc?export=download）
        job.status = "downloading"; db.commit()
        async with httpx.AsyncClient(timeout=60) as session:
            for i, f in enumerate(files):
                img_bytes = await _download_file_direct(session, f["id"])
                if img_bytes:
                    with open(os.path.join(nas_path, f["name"]), "wb") as fp:
                        fp.write(img_bytes)
                job.downloaded_count = i + 1
                if (i + 1) % 50 == 0: db.commit()
                if (i + 1) % 100 == 0: await asyncio.sleep(0.2)
        db.commit()

        saved = len([x for x in os.listdir(nas_path) if not x.startswith(".")])
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
                      "rain_fog_detection": False, "darkness_detection": False})
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
    """從 Google Drive 公開資料夾建立縮時影片 job。

    auth 自行處理整條鏈：用公開 URL 把照片下載到 NAS（/homes/firmness/gdrive_<id>）→ 呼叫
    Spark POST /jobs/nas（Spark 直接從 NAS 讀，無大小/張數上限）。不再委派 Camera Backend
    的 /api/timelapse/gdrive-import（其用需 OAuth 的 files.get?alt=media 對公開資料夾會 403）。
    """
    folder_id = _extract_folder_id(body.folder_url)
    if not folder_id:
        raise HTTPException(400, "無效的 Google Drive 資料夾連結，請確認貼上的是「資料夾」分享連結")

    job = GDriveJob(
        user_id=current_user.id, folder_url=body.folder_url,
        status="pending", fps=body.fps, resolution=body.resolution,
    )
    db.add(job); db.commit(); db.refresh(job)
    asyncio.create_task(_run_gdrive_nas_pipeline(
        job.id, folder_id, body.fps, body.resolution, body.max_images
    ))
    return {"job_id": job.id, "status": "pending", "message": "已開始：下載照片到 NAS → Spark 生成"}



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
