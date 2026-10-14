"""縮時任務的可見範圍：symotus_admin 看全部，其他角色只看自己的。

背景（2026-10-14）：每支 /jobs 端點都寫死 user_id == current_user.id，
jimmy_admin 登入後看不到 Super Admin 建的 52 筆 Google Drive 任務。管理員本來
就能操作全部相機，任務比照辦理；讀、改、刪都走 _scope_to_user 一個 helper。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database import SessionLocal, engine
from models import GDriveJob, GoogleDriveCredential, TimelapsJob
import routers.jobs as jobs_module
from routers.jobs import router as jobs_router


@pytest.fixture()
def jobs_db(db):
    for t in (GoogleDriveCredential.__table__, GDriveJob.__table__, TimelapsJob.__table__):
        t.drop(bind=engine, checkfirst=True)
        t.create(bind=engine, checkfirst=True)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def jobs_client(jobs_db, monkeypatch):
    # 列表會順手向 Spark 對齊未結束的任務；這裡全部用終態，再把同步整個 no-op 掉。
    async def _no_sync(db, jobs):
        return None
    monkeypatch.setattr(jobs_module, "_sync_jobs_with_spark", _no_sync)
    a = FastAPI()
    a.include_router(jobs_router)
    with TestClient(a, base_url="https://testserver") as c:
        yield c


@pytest.fixture()
def people(make_user):
    return {
        "super": make_user("super_admin", "super@example.com", password="pw", role="symotus_admin"),
        "jimmy": make_user("jimmy_admin", "jimmy@example.com", password="pw", role="symotus_admin"),
        "alice": make_user("alice", "alice@example.com", password="pw", role="end_user"),
        "bob": make_user("bob", "bob@example.com", password="pw", role="end_user"),
    }


def _seed(db, people):
    rows = [
        TimelapsJob(user_id=people["super"].id, job_id="cam-super", camera_name="三軍總醫院",
                    status="completed", percent_complete=100),
        TimelapsJob(user_id=people["alice"].id, job_id="cam-alice", camera_name="alice cam",
                    status="completed", percent_complete=100),
        GDriveJob(user_id=people["super"].id, status="completed", folder_name="原檔",
                  fps=30, resolution="1920x1080", total_images=10, downloaded_count=10),
        GDriveJob(user_id=people["bob"].id, status="completed", folder_name="bob folder",
                  fps=30, resolution="1920x1080", total_images=3, downloaded_count=3),
    ]
    db.add_all(rows)
    db.commit()
    for r in rows:
        db.refresh(r)
    return rows


class TestList:
    def test_另一位管理員看得到全部相機任務(self, jobs_client, jobs_db, people, auth_headers):
        _seed(jobs_db, people)
        r = jobs_client.get("/jobs", headers=auth_headers(people["jimmy"]))
        assert r.status_code == 200
        assert sorted(j["job_id"] for j in r.json()) == ["cam-alice", "cam-super"]

    def test_另一位管理員看得到全部gdrive任務(self, jobs_client, jobs_db, people, auth_headers):
        _seed(jobs_db, people)
        r = jobs_client.get("/jobs/gdrive", headers=auth_headers(people["jimmy"]))
        assert r.status_code == 200
        assert sorted(j["folder_name"] for j in r.json()) == ["bob folder", "原檔"]

    def test_一般用戶仍只看自己的(self, jobs_client, jobs_db, people, auth_headers):
        _seed(jobs_db, people)
        r = jobs_client.get("/jobs", headers=auth_headers(people["alice"]))
        assert [j["job_id"] for j in r.json()] == ["cam-alice"]
        r = jobs_client.get("/jobs/gdrive", headers=auth_headers(people["bob"]))
        assert [j["folder_name"] for j in r.json()] == ["bob folder"]
        r = jobs_client.get("/jobs/gdrive", headers=auth_headers(people["alice"]))
        assert r.json() == []


class TestSingle:
    def test_管理員可讀他人gdrive任務_一般用戶不行(self, jobs_client, jobs_db, people, auth_headers):
        rows = _seed(jobs_db, people)
        bob_job = rows[3]
        r = jobs_client.get(f"/jobs/gdrive/{bob_job.id}", headers=auth_headers(people["jimmy"]))
        assert r.status_code == 200
        assert r.json()["job_id"] == bob_job.id
        r = jobs_client.get(f"/jobs/gdrive/{bob_job.id}", headers=auth_headers(people["alice"]))
        assert r.status_code == 404

    def test_管理員可刪他人相機任務_一般用戶不行(self, jobs_client, jobs_db, people, auth_headers):
        _seed(jobs_db, people)
        r = jobs_client.delete("/jobs/cam-super", headers=auth_headers(people["alice"]))
        assert r.status_code == 404
        r = jobs_client.delete("/jobs/cam-super", headers=auth_headers(people["jimmy"]))
        assert r.status_code == 200
        assert jobs_db.query(TimelapsJob).filter_by(job_id="cam-super").first() is None
