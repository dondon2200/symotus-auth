"""縮時任務：建立者欄位、公開分享影片、完成時 LINE 通知（2026-10-14）。

- owner：symotus_admin 看全部任務後，列表要能分辨是誰建的（full_name 優先，否則 username）。
- share：POST /jobs/share 對已完成任務簽到期 ticket；GET /jobs/public/{token} 不需登入回中繼資料，
  /video 串流自 Spark。非擁有者（非 admin）不能分享別人的任務；未完成不能分享。
- notify：任務轉終態時推 LINE 給建立者的 is_active LINE 綁定；同任務 10 分鐘內只推一次；
  完成時間過舊不推；沒 LINE 綁定不推。
"""
import asyncio
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database import SessionLocal, engine
from models import GDriveJob, GoogleDriveCredential, TimelapsJob, UserLineAccount
import routers.jobs as jobs_module
from routers.jobs import router as jobs_router
from auth import create_video_share_ticket, decode_video_share_ticket
from services import job_notifier

from tests.test_jobs_admin_visibility import jobs_db, people, _seed  # noqa: F401

VIDEO = b"\x00\x01\x02\x03" * 64


def _fake_spark(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/download"):
        return httpx.Response(200, content=VIDEO,
                              headers={"accept-ranges": "bytes", "content-type": "video/mp4"})
    # 狀態查詢：依 job id 回預設
    job_id = path.rsplit("/", 1)[-1]
    payload = _fake_spark.status.get(job_id)
    if payload is None:
        return httpx.Response(404, json={})
    return httpx.Response(200, json=payload)


@pytest.fixture()
def share_client(jobs_db, monkeypatch):  # noqa: F811
    for t in (UserLineAccount.__table__,):
        t.drop(bind=engine, checkfirst=True)
        t.create(bind=engine, checkfirst=True)
    _fake_spark.status = {}
    real_client = httpx.AsyncClient
    monkeypatch.setattr(jobs_module.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(_fake_spark), **kw))
    a = FastAPI()
    a.include_router(jobs_router)
    with TestClient(a, base_url="https://testserver") as c:
        yield c
    monkeypatch.setattr(jobs_module.httpx, "AsyncClient", real_client)


@pytest.fixture(autouse=True)
def _reset_notifier(monkeypatch):
    job_notifier._recent.clear()
    sent: list[tuple] = []

    async def _fake_push(line_user_id, title, status, url):
        sent.append((line_user_id, title, status, url))
        return True

    monkeypatch.setattr(job_notifier, "send_job_push", _fake_push)
    yield sent
    job_notifier._recent.clear()


def _bind_line(db, user, line_id, active=True):
    db.add(UserLineAccount(user_id=user.id, line_user_id=line_id, is_active=active))
    db.commit()


async def _drain():
    """等 fire-and-forget 的推播任務跑完。"""
    for _ in range(5):
        pending = [t for t in job_notifier._pending if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


# ── owner ────────────────────────────────────────────────────────────────────

class TestOwner:
    def test_列表帶建立者名稱(self, share_client, jobs_db, people, auth_headers):
        _seed(jobs_db, people)
        # people 來自 conftest 的另一個 session；要用 jobs_db 自己的 session 改才會寫進 DB
        from models import User
        jobs_db.query(User).filter(User.id == people["alice"].id).update({"full_name": "Alice Wang"})
        jobs_db.commit()
        r = share_client.get("/jobs", headers=auth_headers(people["jimmy"]))
        by_id = {j["job_id"]: j for j in r.json()}
        assert by_id["cam-alice"]["owner_name"] == "Alice Wang"
        assert by_id["cam-alice"]["owner_id"] == people["alice"].id
        assert by_id["cam-super"]["owner_name"] == "super_admin"   # 沒 full_name 退回 username

    def test_gdrive列表帶建立者(self, share_client, jobs_db, people, auth_headers):
        _seed(jobs_db, people)
        r = share_client.get("/jobs/gdrive", headers=auth_headers(people["jimmy"]))
        names = {j["folder_name"]: j["owner_name"] for j in r.json()}
        assert names == {"原檔": "super_admin", "bob folder": "bob"}


# ── share ────────────────────────────────────────────────────────────────────

class TestShare:
    def test_ticket_roundtrip(self):
        tok, exp = create_video_share_ticket("camera", "bv-1", 7)
        kind, key, exp2 = decode_video_share_ticket(tok)
        assert (kind, key) == ("camera", "bv-1")
        assert abs((exp2 - exp).total_seconds()) < 2
        assert exp > datetime.utcnow() + timedelta(days=6)

    def test_擁有者可分享並公開觀看(self, share_client, jobs_db, people, auth_headers):
        rows = _seed(jobs_db, people)
        bob_job = rows[3]
        bob_job.spark_job_id = "spark-bob"; jobs_db.commit()
        r = share_client.post("/jobs/share", json={"kind": "gdrive", "job_id": str(bob_job.id)},
                              headers=auth_headers(people["bob"]))
        assert r.status_code == 200, r.text
        url = r.json()["url"]
        assert "/video/" in url and r.json()["days"] == 7
        token = url.rsplit("/", 1)[-1]

        meta = share_client.get(f"/jobs/public/{token}")
        assert meta.status_code == 200
        assert meta.json()["title"] == "bob folder"
        assert meta.json()["kind"] == "gdrive"
        assert meta.json()["video_url"].endswith(f"/jobs/public/{token}/video")

        v = share_client.get(f"/jobs/public/{token}/video")
        assert v.status_code == 200
        assert v.content == VIDEO
        assert v.headers["content-disposition"].startswith("inline")
        d = share_client.get(f"/jobs/public/{token}/video?download=1")
        assert d.headers["content-disposition"].startswith("attachment")

    def test_相機任務也能分享(self, share_client, jobs_db, people, auth_headers):
        _seed(jobs_db, people)
        r = share_client.post("/jobs/share", json={"kind": "camera", "job_id": "cam-super", "days": 3},
                              headers=auth_headers(people["super"]))
        assert r.status_code == 200
        token = r.json()["url"].rsplit("/", 1)[-1]
        meta = share_client.get(f"/jobs/public/{token}").json()
        assert meta["title"] == "三軍總醫院" and meta["kind"] == "camera"

    def test_非擁有者不能分享_admin可以(self, share_client, jobs_db, people, auth_headers):
        _seed(jobs_db, people)
        r = share_client.post("/jobs/share", json={"kind": "camera", "job_id": "cam-super"},
                              headers=auth_headers(people["alice"]))
        assert r.status_code == 404
        r = share_client.post("/jobs/share", json={"kind": "camera", "job_id": "cam-super"},
                              headers=auth_headers(people["jimmy"]))
        assert r.status_code == 200

    def test_未完成不能分享_天數超限422(self, share_client, jobs_db, people, auth_headers):
        jobs_db.add(TimelapsJob(user_id=people["alice"].id, job_id="cam-run", status="processing"))
        jobs_db.commit()
        r = share_client.post("/jobs/share", json={"kind": "camera", "job_id": "cam-run"},
                              headers=auth_headers(people["alice"]))
        assert r.status_code == 404
        r = share_client.post("/jobs/share", json={"kind": "camera", "job_id": "cam-alice", "days": 99},
                              headers=auth_headers(people["alice"]))
        assert r.status_code == 422

    def test_假token與過期404(self, share_client):
        assert share_client.get("/jobs/public/not-a-token").status_code == 404
        tok, _ = create_video_share_ticket("camera", "cam-missing", 1)
        assert share_client.get(f"/jobs/public/{tok}").status_code == 404   # 任務不存在

    def test_任務刪除後連結失效(self, share_client, jobs_db, people, auth_headers):
        _seed(jobs_db, people)
        tok, _ = create_video_share_ticket("camera", "cam-alice", 1)
        assert share_client.get(f"/jobs/public/{tok}").status_code == 200
        share_client.delete("/jobs/cam-alice", headers=auth_headers(people["alice"]))
        assert share_client.get(f"/jobs/public/{tok}").status_code == 404


# ── notify ───────────────────────────────────────────────────────────────────

class TestNotify:
    def test_轉完成時推給建立者的有效LINE(self, share_client, jobs_db, people, auth_headers, _reset_notifier):
        sent = _reset_notifier
        _bind_line(jobs_db, people["alice"], "U-alice-active")
        _bind_line(jobs_db, people["alice"], "U-alice-old", active=False)
        jobs_db.add(TimelapsJob(user_id=people["alice"].id, job_id="bv-9", camera_name="工地 A",
                                status="processing", percent_complete=50))
        jobs_db.commit()
        _fake_spark.status["bv-9"] = {"status": "completed", "percent_complete": 100,
                                      "completed_at": datetime.utcnow().isoformat() + "Z"}

        # jimmy 看列表觸發同步 → 通知的是 alice，不是 jimmy
        r = share_client.get("/jobs", headers=auth_headers(people["jimmy"]))
        assert r.status_code == 200
        assert [j for j in r.json() if j["job_id"] == "bv-9"][0]["status"] == "completed"
        asyncio.run(_drain())
        assert len(sent) == 1
        lid, title, status, url = sent[0]
        assert lid == "U-alice-active" and title == "工地 A" and status == "completed"
        assert url.endswith("/jobs?job=cam:bv-9")

        # 再看一次列表：已是終態不再同步，也不會重複推
        share_client.get("/jobs", headers=auth_headers(people["alice"]))
        asyncio.run(_drain())
        assert len(sent) == 1

    def test_同任務十分鐘內只推一次(self, jobs_db, people, _reset_notifier):
        sent = _reset_notifier
        _bind_line(jobs_db, people["bob"], "U-bob")

        async def go():
            ok1 = job_notifier.schedule_job_notification(jobs_db, "gdrive", 7, people["bob"].id, "x", "completed")
            ok2 = job_notifier.schedule_job_notification(jobs_db, "gdrive", 7, people["bob"].id, "x", "completed")
            await _drain()
            return ok1, ok2

        assert asyncio.run(go()) == (True, False)
        assert len(sent) == 1

    def test_沒綁LINE或完成時間過舊不推(self, jobs_db, people, _reset_notifier):
        sent = _reset_notifier
        _bind_line(jobs_db, people["bob"], "U-bob")

        async def go():
            a = job_notifier.schedule_job_notification(jobs_db, "camera", "k1", people["alice"].id, "x", "completed")
            stale = datetime.utcnow() - timedelta(days=3)
            b = job_notifier.schedule_job_notification(jobs_db, "camera", "k2", people["bob"].id, "x", "completed", stale)
            c = job_notifier.schedule_job_notification(jobs_db, "camera", "k3", people["bob"].id, "x", "processing")
            await _drain()
            return a, b, c

        assert asyncio.run(go()) == (False, False, False)
        assert sent == []

    def test_失敗也會通知(self, jobs_db, people, _reset_notifier):
        sent = _reset_notifier
        _bind_line(jobs_db, people["bob"], "U-bob")

        async def go():
            job_notifier.schedule_job_notification(jobs_db, "gdrive", 3, people["bob"].id, "bob folder", "failed")
            await _drain()

        asyncio.run(go())
        assert sent[0][2] == "failed"
        payload = job_notifier.build_job_push_payload("U", "bob folder", "failed", "https://x/jobs?job=gd:3")
        assert "失敗" in payload["messages"][0]["altText"]

    def test_gdrive輪詢轉完成會推(self, share_client, jobs_db, people, auth_headers, _reset_notifier):
        sent = _reset_notifier
        _bind_line(jobs_db, people["bob"], "U-bob")
        job = GDriveJob(user_id=people["bob"].id, status="processing", spark_job_id="spark-gd",
                        folder_name="工地 B", total_images=5, downloaded_count=5)
        jobs_db.add(job); jobs_db.commit(); jobs_db.refresh(job)
        _fake_spark.status["spark-gd"] = {"status": "completed", "percent_complete": 100}

        r = share_client.get(f"/jobs/gdrive/{job.id}", headers=auth_headers(people["bob"]))
        assert r.status_code == 200 and r.json()["status"] == "completed"
        asyncio.run(_drain())
        assert [s[:3] for s in sent] == [("U-bob", "工地 B", "completed")]

    def test_背景同步一次會把兩種任務對齊(self, share_client, jobs_db, people, _reset_notifier):
        """services.job_notifier.sync_once：不靠任何前端請求也會轉態並推播。"""
        sent = _reset_notifier
        _bind_line(jobs_db, people["alice"], "U-alice")
        _bind_line(jobs_db, people["bob"], "U-bob")
        jobs_db.add(TimelapsJob(user_id=people["alice"].id, job_id="bv-bg", camera_name="背景相機",
                                status="processing"))
        gd = GDriveJob(user_id=people["bob"].id, status="processing", spark_job_id="spark-bg", folder_name="背景 GD")
        jobs_db.add(gd); jobs_db.commit()
        _fake_spark.status["bv-bg"] = {"status": "completed", "percent_complete": 100,
                                       "completed_at": datetime.utcnow().isoformat() + "Z"}
        _fake_spark.status["spark-bg"] = {"status": "failed", "error": "boom"}

        async def go():
            await job_notifier.sync_once()
            await _drain()

        asyncio.run(go())
        jobs_db.expire_all()
        assert jobs_db.query(TimelapsJob).filter_by(job_id="bv-bg").one().status == "completed"
        gd_row = jobs_db.query(GDriveJob).filter_by(spark_job_id="spark-bg").one()
        assert gd_row.status == "failed" and gd_row.error_message == "boom"
        assert sorted(s[:3] for s in sent) == [("U-alice", "背景相機", "completed"), ("U-bob", "背景 GD", "failed")]
