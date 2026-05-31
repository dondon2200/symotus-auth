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

# ── Google Drive 縮時影片 ────────────────────────────────────────────────────────

import re
import httpx

SPARK_API_URL = "https://user.symotus.com/spark"
SPARK_API_KEY = "9ad3343a32508c209152a450f601b990176fa4d41c94c27330e448b1a86826c2"
GDRIVE_API = "https://www.googleapis.com/drive/v3"
GDRIVE_KEY = ""  # 公開資料夾不需要 API key，用空字串即可


class GDriveJobRequest(BaseModel):
    gdrive_url: str
    fps: int = 24
    resolution: Optional[str] = "1080p"
    rain_fog_detection: bool = True
    darkness_detection: bool = True
    image_recovery: bool = True
    stabilization: bool = True
    start_date: Optional[str] = None
    end_date: Optional[str] = None


def extract_folder_id(url: str) -> Optional[str]:
    """從 Google Drive URL 解析出 folder ID"""
    patterns = [
        r"drive\.google\.com/drive/folders/([a-zA-Z0-9_-]+)",
        r"drive\.google\.com/drive/u/\d+/folders/([a-zA-Z0-9_-]+)",
        r"id=([a-zA-Z0-9_-]+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


@router.post("/gdrive")
async def create_gdrive_job(
    body: GDriveJobRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """從 Google Drive 公開資料夾建立縮時影片 job"""
    folder_id = extract_folder_id(body.gdrive_url)
    if not folder_id:
        raise HTTPException(400, "無法解析 Google Drive 連結，請確認連結格式正確")

    # 1. 用 Google Drive API 列出資料夾內的圖片
    async with httpx.AsyncClient(timeout=30) as client:
        # 查資料夾內的圖片檔案
        list_resp = await client.get(
            f"{GDRIVE_API}/files",
            params={
                "q": f"'{folder_id}' in parents and mimeType contains 'image/' and trashed=false",
                "fields": "files(id,name,mimeType,size),nextPageToken",
                "orderBy": "name",
                "pageSize": "1000",
                "key": GDRIVE_KEY,
            }
        )
        if list_resp.status_code == 403 or list_resp.status_code == 404:
            raise HTTPException(400, "無法讀取此資料夾。請確認：1) 資料夾已設為「知道連結的人都可以查看」2) 連結正確且未過期")
        if list_resp.status_code != 200:
            raise HTTPException(400, "連結讀取失敗，請確認 Google Drive 連結是否正確")

        files = list_resp.json().get("files", [])
        if not files:
            raise HTTPException(400, "此資料夾裡找不到任何圖片，請確認資料夾內有 JPG 或 PNG 格式的照片")

        image_count = len(files)
        MAX_IMAGES = 200  # 記憶體限制，超過需要後端工程師支援 NAS 方案

        if image_count > MAX_IMAGES:
            raise HTTPException(400, f"此資料夾有 {image_count} 張照片，目前單次最多支援 {MAX_IMAGES} 張。請縮小日期範圍或減少照片數量後再試")

        # 2. 下載所有圖片並準備 multipart
        images_data = []
        for f in files[:MAX_IMAGES]:  # 最多 MAX_IMAGES 張
            dl = await client.get(
                f"{GDRIVE_API}/files/{f['id']}",
                params={"alt": "media", "key": GDRIVE_KEY}
            )
            if dl.status_code == 200:
                images_data.append((f["name"], dl.content, f.get("mimeType", "image/jpeg")))

        if not images_data:
            raise HTTPException(400, "照片下載失敗。請確認資料夾的分享設定是「知道連結的人都可以查看」，並確認照片格式為 JPG 或 PNG")

        # 3. 送 Spark API
        callback_url = f"https://symotus-auth.onrender.com/jobs/internal/{{job_id}}"

        # 準備 multipart form data
        import uuid
        job_ref = f"gdrive_{folder_id}_{uuid.uuid4().hex[:8]}"

        files_payload = [
            ("images", (name, data, mime))
            for name, data, mime in images_data
        ]

        spark_resp = await client.post(
            f"{SPARK_API_URL}/jobs",
            params={"api_key": SPARK_API_KEY},
            data={
                "callback_url": callback_url,
                "fps": str(body.fps),
                "reference": job_ref,
            },
            files=files_payload,
            timeout=120,
        )

        if spark_resp.status_code not in [200, 201]:
            raise HTTPException(500, "影片生成服務暫時無法使用，請稍後再試")

        spark_data = spark_resp.json()
        spark_job_id = spark_data.get("job_id")

    # 4. 存到 Auth Service DB
    import uuid as _uuid
    job = TimelapsJob(
        job_id=spark_job_id or _uuid.uuid4().hex,
        user_id=current_user.id,
        camera_id=None,
        camera_name=f"Google Drive ({image_count} 張照片)",
        serial_id=folder_id,
        start_date=body.start_date,
        end_date=body.end_date,
        fps=body.fps,
        resolution=body.resolution,
        status="processing",
        image_count=image_count,
    )
    db.add(job)
    db.commit()

    return {
        "job_id": job.job_id,
        "status": "processing",
        "image_count": image_count,
        "message": f"已開始處理 {image_count} 張照片"
    }
