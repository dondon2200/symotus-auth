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

# ── Google Drive 縮時影片（Auth Service 自己處理，不走 Camera Backend）──────────

import re
import httpx

SPARK_API_URL = "https://user.symotus.com/spark"
SPARK_API_KEY = "9ad3343a32508c209152a450f601b990176fa4d41c94c27330e448b1a86826c2"
GDRIVE_API_KEY = "AIzaSyAj-LJs2lbT7pjh0FFkOw-rRH-OMtMl3c4"
GDRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"


def _extract_folder_id(folder_url: str) -> Optional[str]:
    """從 Google Drive 資料夾連結解析 folder id"""
    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", folder_url)
    return m.group(1) if m else None


async def _list_drive_images(folder_id: str, max_images: Optional[int] = None) -> list[dict]:
    """列出 Google Drive 資料夾內的圖片檔案"""
    files = []
    page_token = None
    limit = max_images or 5000

    async with httpx.AsyncClient(timeout=30) as client:
        while len(files) < limit:
            params = {
                "q": f"'{folder_id}' in parents and trashed=false",
                "fields": "nextPageToken,files(id,name,mimeType)",
                "pageSize": min(1000, limit - len(files)),
                "key": GDRIVE_API_KEY,
                "orderBy": "name",
            }
            if page_token:
                params["pageToken"] = page_token

            resp = await client.get(GDRIVE_FILES_URL, params=params)
            if resp.status_code != 200:
                raise HTTPException(400, f"無法列出 Google Drive 資料夾內容（{resp.status_code}）")

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


async def _download_drive_file(file_id: str) -> bytes:
    """下載單張 Google Drive 圖片"""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{GDRIVE_FILES_URL}/{file_id}",
            params={"alt": "media", "key": GDRIVE_API_KEY},
        )
        if resp.status_code != 200:
            raise Exception(f"下載失敗 {file_id}: {resp.status_code}")
        return resp.content


class GDriveJobRequest(BaseModel):
    folder_url: str
    fps: int = 30
    resolution: Optional[str] = "1920x1080"
    rain_fog_detection: bool = True
    darkness_detection: bool = True
    image_recovery: bool = False
    stabilization: bool = False
    max_images: Optional[int] = None


@router.post("/gdrive")
async def create_gdrive_job(
    body: GDriveJobRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """從 Google Drive 公開資料夾建立縮時影片 job（Auth Service 自己下載後送 Spark）"""
    # 1. 解析 folder id
    folder_id = _extract_folder_id(body.folder_url)
    if not folder_id:
        raise HTTPException(400, "無法解析 Google Drive 資料夾連結，請確認格式正確")

    # 2. 列出圖片
    try:
        files = await _list_drive_images(folder_id, body.max_images)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"無法讀取資料夾內容：{e}")

    if not files:
        raise HTTPException(400, "資料夾內沒有找到圖片")
    if len(files) < 100:
        raise HTTPException(400, f"圖片數量不足（{len(files)} 張），縮時影片至少需要 100 張")

    # 3. 下載所有圖片並組成 multipart
    callback_url = "https://symotus-auth.onrender.com/jobs/gdrive/callback"
    form_data = []
    errors = 0
    async with httpx.AsyncClient(timeout=60) as client:
        for f in files:
            try:
                img_bytes = await _download_drive_file(f["id"])
                form_data.append(("images", (f["name"], img_bytes, "image/jpeg")))
            except Exception:
                errors += 1
                if errors > len(files) * 0.2:  # 超過 20% 失敗就放棄
                    raise HTTPException(500, "下載圖片時發生太多錯誤，請稍後再試")

        if not form_data:
            raise HTTPException(500, "所有圖片下載失敗")

        # 4. 送 Spark
        spark_resp = await client.post(
            f"{SPARK_API_URL}/jobs",
            headers={"x-api-key": SPARK_API_KEY},
            data={"callback_url": callback_url, "fps": str(body.fps)},
            files=form_data,
            timeout=120,
        )

    if spark_resp.status_code not in [200, 202]:
        raise HTTPException(500, f"影片生成服務錯誤：{spark_resp.status_code}")

    spark_data = spark_resp.json()
    return {
        "job_id": spark_data.get("job_id"),
        "status": spark_data.get("status", "processing"),
        "image_count": spark_data.get("image_count", len(form_data)),
        "message": f"已上傳 {len(form_data)} 張照片，開始生成縮時影片",
    }


@router.post("/gdrive/callback")
async def gdrive_callback(request: Request, db: Session = Depends(get_db)):
    """接收 Spark 完成通知（server-to-server）"""
    # 暫時只記 log，之後可以推通知給用戶
    body = await request.json()
    return {"message": "received"}


@router.get("/gdrive/{job_id}")
async def get_gdrive_job(
    job_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """查詢 Google Drive 縮時 job 狀態（直接查 Spark）"""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{SPARK_API_URL}/jobs/{job_id}",
            headers={"x-api-key": SPARK_API_KEY},
        )
    if resp.status_code == 404:
        raise HTTPException(404, "找不到此任務")
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, "查詢失敗")

    data = resp.json()
    status = data.get("status", "processing")
    image_count = data.get("image_count", 0)
    processed = data.get("processed_count", 0)

    if status == "completed":
        percent = 100
    elif status == "failed":
        percent = 0
    elif status in ("processing", "running"):
        percent = int((processed / image_count * 80) + 10) if image_count > 0 else 30
    else:
        percent = 5

    # 影片下載 URL
    video_url = None
    if status == "completed":
        video_url = f"{SPARK_API_URL}/jobs/{job_id}/download?api_key={SPARK_API_KEY}"

    return {
        "job_id": job_id,
        "status": status,
        "percent_complete": percent,
        "image_count": image_count,
        "current_stage": "完成" if status == "completed" else ("失敗" if status == "failed" else "AI 生成影片中"),
        "video_download_url": video_url,
        "error_message": data.get("error_message"),
    }


def _stage_label(status: str, spark_status: str, downloaded: int, total: int) -> str:
    if status == "completed":
        return "完成"
    if status == "failed":
        return "失敗"
    return "處理中"


@router.delete("/gdrive/{job_id}")
async def delete_gdrive_job(
    job_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """刪除 Google Drive 縮時 job"""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.delete(
            f"{SPARK_API_URL}/jobs/{job_id}",
            headers={"x-api-key": SPARK_API_KEY},
        )
    if resp.status_code in [200, 204]:
        return {"message": "已刪除"}
    raise HTTPException(resp.status_code, "刪除失敗")
