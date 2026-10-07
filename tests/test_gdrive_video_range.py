"""GDrive 影片代理的分段讀取（Range）測試。

<video> 播放與拖曳都靠 Range；代理若吞掉它、一律回 200 整檔，大檔會卡在 0:00。
Spark 以 httpx.MockTransport 假造，驗證 Range 有轉過去、206／416 有原樣轉回。
"""
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database import SessionLocal, engine
from models import GoogleDriveCredential, GDriveJob
import routers.jobs as jobs_module
from routers.jobs import router as jobs_router
from auth import create_video_ticket

VIDEO = bytes(range(256)) * 4  # 1024 bytes


def _fake_spark(request: httpx.Request) -> httpx.Response:
    """仿 Starlette FileResponse：有 Range 回 206 片段，超出檔尾回 416。"""
    _fake_spark.seen.append(dict(request.headers))
    rng = request.headers.get("range")
    base = {"accept-ranges": "bytes", "content-type": "video/mp4"}
    if not rng:
        return httpx.Response(200, content=VIDEO, headers=base)
    start_s, end_s = rng.removeprefix("bytes=").split("-")
    start = int(start_s)
    if start >= len(VIDEO):
        return httpx.Response(416, headers={"content-range": f"bytes */{len(VIDEO)}"})
    end = int(end_s) if end_s else len(VIDEO) - 1
    return httpx.Response(206, content=VIDEO[start:end + 1], headers={
        **base, "content-range": f"bytes {start}-{end}/{len(VIDEO)}",
    })


@pytest.fixture()
def video_client(db, monkeypatch):
    for t in (GoogleDriveCredential.__table__, GDriveJob.__table__):
        t.drop(bind=engine, checkfirst=True)
        t.create(bind=engine, checkfirst=True)
    _fake_spark.seen = []
    real_client = httpx.AsyncClient
    monkeypatch.setattr(jobs_module.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(_fake_spark), **kw))
    a = FastAPI()
    a.include_router(jobs_router)
    with TestClient(a, base_url="https://testserver") as c:
        yield c
    monkeypatch.setattr(jobs_module.httpx, "AsyncClient", real_client)


@pytest.fixture()
def video_url(make_user):
    user = make_user("vid1", "vid1@example.com", password="pw")
    s = SessionLocal()
    try:
        job = GDriveJob(user_id=user.id, status="completed", spark_job_id="spark-uuid-1")
        s.add(job); s.commit(); s.refresh(job)
        return f"/jobs/gdrive/{job.id}/video?t={create_video_ticket(user.id, job.id)}"
    finally:
        s.close()


def test_without_range_returns_whole_file(video_client, video_url):
    r = video_client.get(video_url)
    assert r.status_code == 200
    assert r.content == VIDEO
    assert r.headers["accept-ranges"] == "bytes"
    assert "range" not in _fake_spark.seen[-1]


def test_range_is_forwarded_and_206_relayed(video_client, video_url):
    r = video_client.get(video_url, headers={"Range": "bytes=100-199"})
    assert _fake_spark.seen[-1]["range"] == "bytes=100-199"
    assert r.status_code == 206
    assert r.content == VIDEO[100:200]
    assert r.headers["content-range"] == "bytes 100-199/1024"
    assert r.headers["content-length"] == "100"


def test_open_ended_range_for_seek(video_client, video_url):
    r = video_client.get(video_url, headers={"Range": "bytes=1000-"})
    assert r.status_code == 206
    assert r.content == VIDEO[1000:]


def test_unsatisfiable_range_returns_416(video_client, video_url):
    r = video_client.get(video_url, headers={"Range": "bytes=5000-"})
    assert r.status_code == 416
    assert r.headers["content-range"] == "bytes */1024"


def test_api_key_still_sent_and_not_leaked(video_client, video_url):
    r = video_client.get(video_url, headers={"Range": "bytes=0-9"})
    assert _fake_spark.seen[-1]["x-api-key"] == jobs_module.SPARK_API_KEY
    assert "x-api-key" not in r.headers
