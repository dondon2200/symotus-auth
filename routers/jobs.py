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
import httpx
from models import GDriveJob

SPARK_API_URL = "https://user.symotus.com/spark"
SPARK_API_KEY = "9ad3343a32508c209152a450f601b990176fa4d41c94c27330e448b1a86826c2"
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
    """不走 API，用公開 HTTP 下載（避免 API 配額消耗）"""
    try:
        # 先嘗試直接下載
        resp = await session.get(
            GDRIVE_DOWNLOAD_URL,
            params={"export": "download", "id": file_id},
            follow_redirects=True,
            timeout=30,
        )
        if resp.status_code == 200 and len(resp.content) > 1000:
            return resp.content
        # 如果被擋（病毒掃描確認頁面），嘗試帶 confirm
        if b"confirm=" in resp.content:
            import re as _re
            m = _re.search(rb'confirm=([0-9A-Za-z_-]+)', resp.content)
            if m:
                confirm = m.group(1).decode()
                resp2 = await session.get(
                    GDRIVE_DOWNLOAD_URL,
                    params={"export": "download", "id": file_id, "confirm": confirm},
                    follow_redirects=True,
                    timeout=30,
                )
                if resp2.status_code == 200:
                    return resp2.content
        return None
    except Exception:
        return None


async def _run_gdrive_background(job_id: int, files: list[dict], body_fps: int, body_resolution: Optional[str]):
    """背景任務：分批下載照片 → 送 Spark"""
    from database import SessionLocal
    db = SessionLocal()
    try:
        job = db.query(GDriveJob).filter(GDriveJob.id == job_id).first()
        if not job:
            return

        # 下載階段
        job.status = "downloading"
        db.commit()

        downloaded_files = []
        BATCH = 50  # 每批 50 張，避免記憶體爆炸
        async with httpx.AsyncClient(timeout=60) as session:
            for i, f in enumerate(files):
                img_bytes = await _download_file_direct(session, f["id"])
                if img_bytes:
                    downloaded_files.append((f["name"], img_bytes))
                # 每下載一張更新進度
                job.downloaded_count = i + 1
                if (i + 1) % 10 == 0:
                    db.commit()
                # 小間隔避免觸發 Google 速率限制
                if (i + 1) % BATCH == 0:
                    await asyncio.sleep(1)

        job.downloaded_count = len(downloaded_files)
        db.commit()

        if len(downloaded_files) < 100:
            job.status = "failed"
            job.error_message = f"成功下載 {len(downloaded_files)} 張，不足 100 張無法生成縮時影片"
            db.commit()
            return

        # 上傳 Spark 階段
        job.status = "uploading"
        db.commit()

        callback_url = f"https://symotus-auth.onrender.com/jobs/gdrive/callback/{job_id}"
        form_files = [("images", (name, data, "image/jpeg")) for name, data in downloaded_files]

        async with httpx.AsyncClient(timeout=300) as client:
            spark_resp = await client.post(
                f"{SPARK_API_URL}/jobs",
                headers={"x-api-key": SPARK_API_KEY},
                data={"callback_url": callback_url, "fps": str(body_fps)},
                files=form_files,
            )

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
    """從 Google Drive 公開資料夾建立縮時影片 job（背景下載，立即回傳）""

    folder_id = _extract_folder_id(body.folder_url)
    if not folder_id:
        raise HTTPException(400, "無法解析 Google Drive 資料夾連結")

    # 先列出檔案清單（快，只是 API 查詢）
    try:
        files = await _list_drive_images(folder_id, body.max_images)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"無法讀取資料夾：{e}")

    if not files:
        raise HTTPException(400, "資料夾內沒有找到圖片")
    if len(files) < 100:
        raise HTTPException(400, f"圖片數量不足（{len(files)} 張），縮時影片至少需要 100 張")

    # 建立 DB 記錄
    job = GDriveJob(
        user_id=current_user.id,
        folder_url=body.folder_url,
        status="pending",
        total_images=len(files),
        fps=body.fps,
        resolution=body.resolution,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    # 背景執行下載 + 送 Spark
    asyncio.create_task(_run_gdrive_background(job.id, files, body.fps, body.resolution))

    return {
        "job_id": job.id,
        "status": "pending",
        "image_count": len(files),
        "message": f"找到 {len(files)} 張照片，開始背景下載",
    }


@router.post("/gdrive/callback/{job_id}")
async def gdrive_spark_callback(job_id: int, request: Request, db: Session = Depends(get_db)):
    """接收 Spark 完成 callback"""
    data = await request.json()
    job = db.query(GDriveJob).filter(GDriveJob.id == job_id).first()
    if not job:
        return {"message": "not found"}

    spark_status = data.get("status", "")
    if spark_status == "completed":
        job.status = "completed"
        job.video_url = data.get("download_url") or f"{SPARK_API_URL}/jobs/{job.spark_job_id}/download?api_key={SPARK_API_KEY}"
    elif spark_status == "failed":
        job.status = "failed"
        job.error_message = data.get("error_message", "Spark 處理失敗")

    job.updated_at = datetime.utcnow()
    db.commit()
    return {"message": "ok"}


@router.get("/gdrive/{job_id}")
async def get_gdrive_job(
    job_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """查詢 Google Drive 縮時 job 進度"""
    job = db.query(GDriveJob).filter(
        GDriveJob.id == job_id,
        GDriveJob.user_id == current_user.id,
    ).first()
    if not job:
        raise HTTPException(404, "找不到此任務")

    # 如果 Spark 已有 job_id，直接 poll Spark 取最新狀態
    if job.spark_job_id and job.status == "processing":
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                spark_resp = await client.get(
                    f"{SPARK_API_URL}/jobs/{job.spark_job_id}",
                    headers={"x-api-key": SPARK_API_KEY},
                )
            if spark_resp.status_code == 200:
                spark_data = spark_resp.json()
                if spark_data.get("status") == "completed":
                    job.status = "completed"
                    job.video_url = f"{SPARK_API_URL}/jobs/{job.spark_job_id}/download?api_key={SPARK_API_KEY}"
                    db.commit()
                elif spark_data.get("status") == "failed":
                    job.status = "failed"
                    job.error_message = spark_data.get("error_message", "")
                    db.commit()
        except Exception:
            pass

    # 計算進度
    if job.status == "completed":
        percent = 100
    elif job.status == "failed":
        percent = 0
    elif job.status == "downloading":
        percent = int((job.downloaded_count / job.total_images * 60)) if job.total_images > 0 else 5
    elif job.status == "uploading":
        percent = 65
    elif job.status == "processing":
        percent = 75
    else:
        percent = 2

    stage_map = {
        "pending": "準備中",
        "downloading": f"下載照片中（{job.downloaded_count}/{job.total_images}）",
        "uploading": "上傳至處理伺服器中",
        "processing": "AI 生成影片中",
        "completed": "完成",
        "failed": "失敗",
    }

    return {
        "job_id": job.id,
        "status": job.status,
        "percent_complete": percent,
        "image_count": job.total_images,
        "downloaded_count": job.downloaded_count,
        "current_stage": stage_map.get(job.status, "處理中"),
        "video_download_url": job.video_url,
        "error_message": job.error_message,
    }


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
