"""帳號旗標 users.jobs_view_all：可讀取全部縮時影片，但不能改／刪別人的任務。

背景（2026-10-26）：同事要看公司所有縮時影片，但只有 symotus_admin 看得到全部任務，
升 admin 又太大。旗標比照 engineering_mode：admin 在帳號管理切換、寫進 JWT 與 /auth/me；
routers/jobs.py 的讀路徑（列表／單筆／分享）用 _scope_for_read，寫路徑維持 _scope_to_user。
設計：symotus-frontend/docs/superpowers/specs/2026-10-26-jobs-view-all-flag-design.md
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt

from config import settings
from auth import create_access_token
from database import SessionLocal, engine
from models import GDriveJob, GoogleDriveCredential, TimelapsJob
import routers.jobs as jobs_module
from routers.jobs import router as jobs_router
from routers.admin import router as admin_router
from routers.auth import router as auth_router


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
def api(jobs_db, monkeypatch):
    async def _no_sync(db, jobs):
        return None
    monkeypatch.setattr(jobs_module, "_sync_jobs_with_spark", _no_sync)
    a = FastAPI()
    a.include_router(jobs_router)
    a.include_router(admin_router)
    a.include_router(auth_router)
    with TestClient(a, base_url="https://testserver") as c:
        yield c


@pytest.fixture()
def people(make_user, db):
    viewer = make_user("maomao", "maomao@example.com", password="pw", role="end_user")
    viewer.jobs_view_all = True
    db.commit()
    db.refresh(viewer)
    return {
        "admin": make_user("plat_admin", "admin@example.com", password="pw", role="symotus_admin"),
        "viewer": viewer,
        "alice": make_user("alice", "alice@example.com", password="pw", role="end_user"),
    }


def _seed(db, people):
    rows = [
        TimelapsJob(user_id=people["alice"].id, job_id="cam-alice", camera_name="alice cam",
                    status="completed", percent_complete=100),
        TimelapsJob(user_id=people["viewer"].id, job_id="cam-mine", camera_name="my cam",
                    status="completed", percent_complete=100),
        GDriveJob(user_id=people["alice"].id, status="completed", folder_name="alice folder",
                  fps=30, resolution="1920x1080", total_images=3, downloaded_count=3,
                  spark_job_id="spark-alice", job_params="{}"),
    ]
    db.add_all(rows)
    db.commit()
    for r in rows:
        db.refresh(r)
    return rows


def _claims(token: str) -> dict:
    return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])


class TestRead:
    def test_旗標開的end_user看得到全部相機任務(self, api, jobs_db, people, auth_headers):
        _seed(jobs_db, people)
        r = api.get("/jobs", headers=auth_headers(people["viewer"]))
        assert r.status_code == 200
        assert sorted(j["job_id"] for j in r.json()) == ["cam-alice", "cam-mine"]
        # 列表要能分辨建立者
        by_id = {j["job_id"]: j for j in r.json()}
        assert by_id["cam-alice"]["owner_name"] == "alice"

    def test_旗標開的end_user看得到全部gdrive任務與單筆(self, api, jobs_db, people, auth_headers):
        rows = _seed(jobs_db, people)
        r = api.get("/jobs/gdrive", headers=auth_headers(people["viewer"]))
        assert r.status_code == 200
        assert [j["folder_name"] for j in r.json()] == ["alice folder"]
        r = api.get(f"/jobs/gdrive/{rows[2].id}", headers=auth_headers(people["viewer"]))
        assert r.status_code == 200

    def test_旗標開的end_user可簽別人影片的分享連結(self, api, jobs_db, people, auth_headers, monkeypatch):
        rows = _seed(jobs_db, people)
        r = api.post("/jobs/share", headers=auth_headers(people["viewer"]),
                     json={"kind": "gdrive", "job_id": str(rows[2].id)})
        assert r.status_code == 200, r.text
        assert "/video/" in r.json()["url"]

    def test_旗標關的end_user仍只看自己的(self, api, jobs_db, people, auth_headers):
        rows = _seed(jobs_db, people)
        r = api.get("/jobs", headers=auth_headers(people["alice"]))
        assert [j["job_id"] for j in r.json()] == ["cam-alice"]
        r = api.get("/jobs/gdrive", headers=auth_headers(people["alice"]))
        assert [j["folder_name"] for j in r.json()] == ["alice folder"]
        # 沒旗標的人不能簽別人的分享連結
        r = api.post("/jobs/share", headers=auth_headers(people["alice"]),
                     json={"kind": "camera", "job_id": "cam-mine"})
        assert r.status_code == 404


class TestWriteStaysOwnerOnly:
    def test_不能刪別人的相機任務(self, api, jobs_db, people, auth_headers):
        _seed(jobs_db, people)
        r = api.delete("/jobs/cam-alice", headers=auth_headers(people["viewer"]))
        assert r.status_code == 404
        assert jobs_db.query(TimelapsJob).filter_by(job_id="cam-alice").first() is not None

    def test_不能改別人的相機任務(self, api, jobs_db, people, auth_headers):
        _seed(jobs_db, people)
        r = api.put("/jobs/cam-alice", headers=auth_headers(people["viewer"]),
                    json={"percent_complete": 50})
        assert r.status_code == 404

    def test_不能刪或續傳別人的gdrive任務(self, api, jobs_db, people, auth_headers):
        rows = _seed(jobs_db, people)
        gid = rows[2].id
        r = api.delete(f"/jobs/gdrive/{gid}", headers=auth_headers(people["viewer"]))
        assert r.status_code == 404
        r = api.post(f"/jobs/gdrive/{gid}/resume", headers=auth_headers(people["viewer"]), json={})
        assert r.status_code == 404
        assert jobs_db.query(GDriveJob).filter_by(id=gid).first() is not None

    def test_自己的任務仍可刪(self, api, jobs_db, people, auth_headers):
        _seed(jobs_db, people)
        r = api.delete("/jobs/cam-mine", headers=auth_headers(people["viewer"]))
        assert r.status_code == 200


class TestFlagPlumbing:
    def test_新帳號預設關閉_token與me都是false(self, api, people, auth_headers, db):
        alice = people["alice"]
        assert alice.jobs_view_all is False
        assert _claims(create_access_token(alice, db))["jobs_view_all"] is False
        me = api.get("/auth/me", headers=auth_headers(alice)).json()
        assert me["jobs_view_all"] is False

    def test_admin可開關_之後token與me跟著變(self, api, people, auth_headers, db):
        alice = people["alice"]
        r = api.put(f"/admin/users/{alice.id}", headers=auth_headers(people["admin"]),
                    json={"jobs_view_all": True})
        assert r.status_code == 200, r.text
        assert r.json()["jobs_view_all"] is True
        db.refresh(alice)
        assert alice.jobs_view_all is True
        assert _claims(create_access_token(alice, db))["jobs_view_all"] is True
        assert api.get("/auth/me", headers=auth_headers(alice)).json()["jobs_view_all"] is True
        r = api.put(f"/admin/users/{alice.id}", headers=auth_headers(people["admin"]),
                    json={"jobs_view_all": False})
        assert r.status_code == 200
        db.refresh(alice)
        assert alice.jobs_view_all is False

    def test_非布林值被拒(self, api, people, auth_headers):
        r = api.put(f"/admin/users/{people['alice'].id}", headers=auth_headers(people["admin"]),
                    json={"jobs_view_all": "yes"})
        assert r.status_code == 400

    def test_使用者清單帶旗標(self, api, people, auth_headers):
        r = api.get("/admin/users", headers=auth_headers(people["admin"]))
        assert r.status_code == 200
        flags = {u["username"]: u["jobs_view_all"] for u in r.json()}
        assert flags["maomao"] is True
        assert flags["alice"] is False
