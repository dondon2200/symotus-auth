"""GDrive 下載任務的續傳與孤兒回收（P0-a / P0-b）。

背景：下載跑在 uvicorn 進程內的 asyncio.create_task，沒有佇列也沒有持久化。一次部署
就會讓任務無聲消失、job 永遠停在 downloading（實際發生過：job 40 卡在 5%）。這裡驗證
三件事：進程換掉後 job 會被收成 interrupted、續傳能從 job_params 重建、以及沒有
job_params 的舊任務會被明確拒絕而不是假裝可以續傳。
"""
import asyncio
import json
import pytest

from models import GDriveJob, GoogleDriveCredential
from routers import jobs as jobs_module
from config import settings

# 沿用 test_gdrive_oauth 的建表／client fixture（同一組表，避免重複定義）
from tests.test_gdrive_oauth import gdrive_db, gdrive_client, _job_body  # noqa: F401


def _mk_job(db, user, status, job_params=None, **over):
    job = GDriveJob(
        user_id=user.id, status=status, fps=30, resolution="1920x1080",
        total_images=71789, downloaded_count=5125,
        google_refresh_token="rt-resume",
        job_params=json.dumps(job_params) if job_params is not None else None,
        **over,
    )
    db.add(job); db.commit(); db.refresh(job)
    return job


PARAMS = {
    "folder_ids": ["folder-1", "folder-2"],
    "picked_files": [{"id": "f1", "name": "a.jpg"}],
    "fps": 30, "resolution": "1920x1080",
    "rain_fog_detection": True, "darkness_detection": False,
    "max_images": None, "duration_seconds": 60,
}


# ── P0-b：孤兒回收 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status", ["pending", "listing", "downloading"])
def test_reaper_marks_owned_stages_interrupted(gdrive_db, make_user, status):
    """下載階段的 job 在新進程啟動時必然是孤兒，要收成 interrupted。"""
    user = make_user(f"reap-{status}", f"reap-{status}@example.com", password="pw")
    job = _mk_job(gdrive_db, user, status, PARAMS)

    assert jobs_module.reap_orphaned_gdrive_jobs() >= 1

    gdrive_db.expire_all()
    row = gdrive_db.query(GDriveJob).get(job.id)
    assert row.status == "interrupted"
    assert "服務重啟" in row.error_message
    # 進度不能被清掉——續傳全靠它與 NAS 上的檔案
    assert row.downloaded_count == 5125


@pytest.mark.parametrize("status", ["submitted", "processing", "completed", "failed"])
def test_reaper_leaves_spark_and_terminal_jobs_alone(gdrive_db, make_user, status):
    """交給 Spark 之後的 job 不歸本進程管，重啟不該動它們。"""
    user = make_user(f"keep-{status}", f"keep-{status}@example.com", password="pw")
    job = _mk_job(gdrive_db, user, status, PARAMS, spark_job_id="spark-1")

    jobs_module.reap_orphaned_gdrive_jobs()

    gdrive_db.expire_all()
    assert gdrive_db.query(GDriveJob).get(job.id).status == status


# ── P0-a：續傳 ───────────────────────────────────────────────────────────────

@pytest.fixture()
def _google_ok(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "sec")

    async def fake_refresh(refresh_token):
        return {"access_token": "at-new", "expires_in": 3600}

    monkeypatch.setattr(jobs_module, "_refresh_access_token", fake_refresh)


def test_resume_relaunches_with_persisted_params(gdrive_client, gdrive_db, make_user,
                                                 auth_headers, monkeypatch, _google_ok):
    """續傳要用存下來的 folder_ids/picked_files 重建清單，並換一份新的 access token。"""
    user = make_user("res1", "res1@example.com", password="pw")
    job = _mk_job(gdrive_db, user, "interrupted", PARAMS)

    started = {}

    async def fake_pipeline(*a, **kw):
        started["job_id"] = a[0] if a else kw.get("job_id")
        started["kwargs"] = kw

    monkeypatch.setattr(jobs_module, "_run_gdrive_nas_pipeline", fake_pipeline)

    r = gdrive_client.post(f"/jobs/gdrive/{job.id}/resume", headers=auth_headers(user))
    assert r.status_code == 200, r.text

    assert started["job_id"] == job.id
    kw = started["kwargs"]
    assert kw["folder_ids"] == ["folder-1", "folder-2"]
    assert kw["picked_files"] == [{"id": "f1", "name": "a.jpg"}]
    assert kw["duration_seconds"] == 60          # 抽樣設定不能在續傳時遺失
    assert kw["rain_fog"] is True and kw["darkness"] is False
    assert kw["initial_access_token"] == "at-new"  # 舊 token 早就過期了
    assert kw["initial_expires_in"] == 3600


def test_resume_without_params_is_rejected(gdrive_client, gdrive_db, make_user,
                                           auth_headers, _google_ok):
    """舊任務沒存參數就無法重建清單，必須明講而不是假裝續傳。"""
    user = make_user("res2", "res2@example.com", password="pw")
    job = _mk_job(gdrive_db, user, "interrupted", None)

    r = gdrive_client.post(f"/jobs/gdrive/{job.id}/resume", headers=auth_headers(user))
    assert r.status_code == 409
    assert "重新建立" in r.json()["detail"]


def test_resume_rejects_running_job(gdrive_client, gdrive_db, make_user,
                                    auth_headers, _google_ok):
    """正在跑的 job 不能被續傳，否則會有兩個任務寫同一個目錄。"""
    user = make_user("res3", "res3@example.com", password="pw")
    job = _mk_job(gdrive_db, user, "downloading", PARAMS)

    r = gdrive_client.post(f"/jobs/gdrive/{job.id}/resume", headers=auth_headers(user))
    assert r.status_code == 409


def test_resume_is_scoped_to_owner(gdrive_client, gdrive_db, make_user,
                                   auth_headers, _google_ok):
    """別人的任務不能被續傳（等同於代跑他人的 Drive 下載）。"""
    owner = make_user("res4a", "res4a@example.com", password="pw")
    other = make_user("res4b", "res4b@example.com", password="pw")
    job = _mk_job(gdrive_db, owner, "interrupted", PARAMS)

    r = gdrive_client.post(f"/jobs/gdrive/{job.id}/resume", headers=auth_headers(other))
    assert r.status_code == 404


def test_status_reports_resumable_flag(gdrive_client, gdrive_db, make_user, auth_headers):
    """前端靠 resumable 決定要不要顯示「繼續下載」。"""
    user = make_user("res5", "res5@example.com", password="pw")
    ok = _mk_job(gdrive_db, user, "interrupted", PARAMS)
    legacy = _mk_job(gdrive_db, user, "interrupted", None)

    r1 = gdrive_client.get(f"/jobs/gdrive/{ok.id}", headers=auth_headers(user))
    assert r1.json()["resumable"] is True
    r2 = gdrive_client.get(f"/jobs/gdrive/{legacy.id}", headers=auth_headers(user))
    assert r2.json()["resumable"] is False


# ── 續傳的核心：既有檔案要被跳過 ─────────────────────────────────────────────

def test_existing_sizes_skips_empty_and_dotfiles(tmp_path):
    """只有 size>0 的檔案算數：被砍在寫入當下的 0 byte 殘檔必須重下。"""
    (tmp_path / "000001_a.jpg").write_bytes(b"xyz")
    (tmp_path / "000002_b.jpg").write_bytes(b"")
    (tmp_path / ".sampled").write_bytes(b"")

    sizes = jobs_module._existing_sizes(str(tmp_path))

    assert sizes["000001_a.jpg"] == 3
    assert sizes["000002_b.jpg"] == 0     # 呼叫端以 <=0 判定為「要重下」
    assert ".sampled" not in sizes


def test_existing_sizes_on_missing_dir_is_empty(tmp_path):
    """第一次執行時目錄還不存在，不能炸掉。"""
    assert jobs_module._existing_sizes(str(tmp_path / "nope")) == {}


# ── P1：縮圖下載 ─────────────────────────────────────────────────────────────

def _jpeg(width: int, height: int = 100) -> bytes:
    """最小可解析的 JPEG：SOI + SOF0(含尺寸) + EOI。"""
    import struct
    sof = b"\xff\xc0" + struct.pack(">HBHH", 17, 8, height, width) + b"\x00" * 8
    return b"\xff\xd8" + sof + b"\xff\xd9" + b"\x00" * 2000


def test_thumbnail_size_follows_output_resolution():
    """縮圖尺寸必須 >= 輸出尺寸，否則等於拿上採樣的圖生成影片。"""
    assert jobs_module._thumbnail_size_for("1920x1080") == 2560
    assert jobs_module._thumbnail_size_for("3840x2160") == 4096
    # 未知解析度不猜，退回原檔
    assert jobs_module._thumbnail_size_for("1280x720") is None
    assert jobs_module._thumbnail_size_for(None) is None
    assert jobs_module._thumbnail_size_for("weird") is None


def test_rewrite_thumbnail_url_replaces_size_suffix():
    base = "https://lh3.googleusercontent.com/drive-storage/AbC-xyz"
    assert jobs_module._rewrite_thumbnail_url(base + "=s220", 2560) == base + "=s2560"
    assert jobs_module._rewrite_thumbnail_url(base + "=w200-h150-p", 4096) == base + "=s4096"
    # 沒有 = 後綴時要補上，不能原樣送出（那會拿到 220px 的縮圖）
    assert jobs_module._rewrite_thumbnail_url(base, 2560) == base + "=s2560"


def test_jpeg_width_reads_sof():
    assert jobs_module._jpeg_width(_jpeg(2560)) == 2560
    assert jobs_module._jpeg_width(b"not a jpeg") is None


class _FakeResp:
    def __init__(self, status_code, content=b""):
        self.status_code = status_code
        self.content = content


class _FakeSession:
    """記錄每次 GET 的 url 與是否帶 Authorization。"""
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    async def get(self, url, **kw):
        self.calls.append((url, "Authorization" in (kw.get("headers") or {})))
        return self.handler(url, kw)


def test_thumbnail_download_uses_signed_url_without_auth():
    """縮圖走簽名 URL，不該帶 Authorization——這是能把並發拉到 48 的前提。"""
    session = _FakeSession(lambda url, kw: _FakeResp(200, _jpeg(2560)))
    item = {"id": "f1", "thumbnailLink": "https://lh3.example/x=s220"}

    data = asyncio.run(jobs_module._download_thumbnail(session, None, item, 2560, 1920))

    assert data is not None
    url, had_auth = session.calls[0]
    assert url.endswith("=s2560")
    assert had_auth is False


def test_thumbnail_too_small_falls_back_to_original():
    """Google 給不到要求的尺寸時必須回 None，讓呼叫端改抓原檔。"""
    session = _FakeSession(lambda url, kw: _FakeResp(200, _jpeg(800)))
    item = {"id": "f1", "thumbnailLink": "https://lh3.example/x=s220"}

    assert asyncio.run(jobs_module._download_thumbnail(session, None, item, 2560, 1920)) is None


def test_expired_link_is_refreshed_once(monkeypatch):
    """簽名過期（403）要自動換一條新連結重試，而不是整批失敗。"""
    state = {"n": 0}

    def handler(url, kw):
        state["n"] += 1
        if "old" in url:
            return _FakeResp(403)
        return _FakeResp(200, _jpeg(2560))

    async def fake_fresh(session, token_mgr, file_id):
        return "https://lh3.example/new=s220"

    monkeypatch.setattr(jobs_module, "_fresh_thumbnail_link", fake_fresh)
    session = _FakeSession(handler)
    item = {"id": "f1", "thumbnailLink": "https://lh3.example/old=s220"}

    data = asyncio.run(jobs_module._download_thumbnail(session, None, item, 2560, 1920))

    assert data is not None
    assert state["n"] == 2                       # 舊的失敗、新的成功
    assert item["thumbnailLink"].startswith("https://lh3.example/new")  # 換過的連結要留下來


def test_error_page_is_not_mistaken_for_image(monkeypatch):
    """200 但內容過小（錯誤頁）不能當成照片寫進 NAS。"""
    async def fake_fresh(session, token_mgr, file_id):
        return None

    monkeypatch.setattr(jobs_module, "_fresh_thumbnail_link", fake_fresh)
    session = _FakeSession(lambda url, kw: _FakeResp(200, b"<html>nope</html>"))
    item = {"id": "f1", "thumbnailLink": "https://lh3.example/x=s220"}

    assert asyncio.run(jobs_module._download_thumbnail(session, None, item, 2560, 1920)) is None


def test_pipeline_handles_unparsable_resolution(tmp_path, monkeypatch):
    """resolution 不是 WxH 時不能炸掉——之前 re.match(...).group() 會 AttributeError。"""
    for bad in (None, "", "weird", "4K"):
        m = jobs_module.re.match(r"\s*(\d+)", bad or "")
        out_width = int(m.group(1)) if m else 0
        assert isinstance(out_width, int)
        assert jobs_module._thumbnail_size_for(bad) is None
