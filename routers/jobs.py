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

CAMERA_BACKEND_URL = "https://user.symotus.com"
CAMERA_SERVICE_KEY = "9ad3343a32508c209152a450f601b990176fa4d41c94c27330e448b1a86826c2"


class GDriveJobRequest(BaseModel):
    folder_url: str
    fps: int = 30
    resolution: Optional[str] = "1920x1080"
    rain_fog_detection: bool = True
    darkness_detection: bool = True
    image_recovery: bool = False
    stabilization: bool = False
    max_images: Optional[int] = None


async def get_camera_token_for_user(user: User) -> str:
    """換取 Camera Backend token"""
    if not user.camera_email:
        return ""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{CAMERA_BACKEND_URL}/internal/auth/token",
            headers={"x-service-key": CAMERA_SERVICE_KEY},
            json={"user_id": 0, "email": user.camera_email, "role": user.role},
        )
        if resp.status_code == 200:
            return resp.json().get("access_token", "")
    return ""


@router.post("/gdrive")
async def create_gdrive_job(
    body: GDriveJobRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """從 Google Drive 公開資料夾建立縮時影片 job"""
    cam_token = await get_camera_token_for_user(current_user)
    if not cam_token:
        raise HTTPException(503, "無法連接影片服務，請稍後再試")

    async with httpx.AsyncClient(timeout=30) as client:
        payload = {
            "folder_url": body.folder_url,
            "fps": body.fps,
            "resolution": body.resolution,
            "rain_fog_detection": body.rain_fog_detection,
            "darkness_detection": body.darkness_detection,
            "image_recovery": body.image_recovery,
            "stabilization": body.stabilization,
        }
        if body.max_images:
            payload["max_images"] = body.max_images

        resp = await client.post(
            f"{CAMERA_BACKEND_URL}/api/timelapse/gdrive-import",
            headers={"Authorization": f"Bearer {cam_token}", "Content-Type": "application/json"},
            json=payload,
        )

    if resp.status_code == 400:
        detail = resp.json().get("detail", "")
        if "資料夾" in detail or "id" in detail.lower():
            raise HTTPException(400, "無法讀取此 Google Drive 資料夾，請確認連結正確且已設為公開")
        raise HTTPException(400, detail)
    if resp.status_code not in [200, 201]:
        raise HTTPException(500, "影片生成服務暫時無法使用，請稍後再試")

    data = resp.json()
    return {
        "job_id": data.get("id"),
        "status": data.get("status", "processing"),
        "image_count": data.get("image_count", 0),
        "message": "已開始下載照片並生成影片",
    }


@router.get("/gdrive/{job_id}")
async def get_gdrive_job(
    job_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """查詢 Google Drive 縮時 job 狀態"""
    cam_token = await get_camera_token_for_user(current_user)
    if not cam_token:
        raise HTTPException(503, "無法連接影片服務")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{CAMERA_BACKEND_URL}/api/timelapse/jobs/{job_id}",
            headers={"Authorization": f"Bearer {cam_token}"},
        )
    if resp.status_code == 404:
        raise HTTPException(404, "找不到此任務")
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, "查詢失敗")

    data = resp.json()
    # 計算進度
    image_count = data.get("image_count", 0)
    downloaded = data.get("downloaded_count", 0)
    spark_status = data.get("spark_status") or ""
    status = data.get("status", "processing")

    # 進度計算：下載佔 40%，spark 處理佔 60%
    if status == "completed":
        percent = 100
    elif status == "failed":
        percent = 0
    elif spark_status in ("processing", "running"):
        percent = 70
    elif spark_status == "completed":
        percent = 95
    elif image_count > 0 and downloaded > 0:
        percent = int((downloaded / image_count) * 40)
    else:
        percent = 5

    return {
        "job_id": data.get("id"),
        "status": status,
        "percent_complete": percent,
        "image_count": image_count,
        "downloaded_count": downloaded,
        "current_stage": _stage_label(status, spark_status, downloaded, image_count),
        "video_download_url": data.get("video_download_url"),
        "error_message": data.get("error_message"),
    }


def _stage_label(status: str, spark_status: str, downloaded: int, total: int) -> str:
    if status == "completed":
        return "完成"
    if status == "failed":
        return "失敗"
    if status == "downloading":
        return f"下載照片中（{downloaded}/{total}）"
    if status == "processing" and not spark_status:
        return "準備中"
    if spark_status in ("processing", "running"):
        return "AI 生成影片中"
    if spark_status == "completed":
        return "後處理中"
    return "處理中"


@router.delete("/gdrive/{job_id}")
async def delete_gdrive_job(
    job_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """刪除 Google Drive 縮時 job"""
    cam_token = await get_camera_token_for_user(current_user)
    if not cam_token:
        raise HTTPException(503, "無法連接影片服務")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.delete(
            f"{CAMERA_BACKEND_URL}/api/timelapse/jobs/{job_id}",
            headers={"Authorization": f"Bearer {cam_token}"},
        )

    if resp.status_code == 204 or resp.status_code == 200:
        return {"message": "已刪除"}
    raise HTTPException(resp.status_code, "刪除失敗")
