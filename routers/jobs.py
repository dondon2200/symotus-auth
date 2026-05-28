from fastapi import APIRouter, Depends, HTTPException
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
