"""GDrive 下載任務的續傳與孤兒回收（P0-a / P0-b）。

背景：下載跑在 uvicorn 進程內的 asyncio.create_task，沒有佇列也沒有持久化。一次部署
就會讓任務無聲消失、job 永遠停在 downloading（實際發生過：job 40 卡在 5%）。這裡驗證
三件事：進程換掉後 job 會被收成 interrupted、續傳能從 job_params 重建、以及沒有
job_params 的舊任務會被明確拒絕而不是假裝可以續傳。
"""
import asyncio
import json
import time
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
    """縮圖走簽名 URL，不該帶 Authorization——這是能把並發拉到數十路的前提。"""
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


# ── 記憶體回歸：下載不得超前寫檔 ─────────────────────────────────────────────

def test_download_does_not_outrun_writes(monkeypatch):
    """已下載但尚未寫出的圖片數量必須被 sem 上限夾住。

    回歸自 job 41：寫檔原本在 sem 之外，下載（數十路、~1 MB/張）遠遠超前 NFS 寫入，
    每張未寫出的圖都以 bytes 留在 RAM，30 分鐘內吃到 27 GB / 30 GB 幾乎 OOM。
    """
    CONC, TOTAL = 4, 60
    inflight = {"now": 0, "peak": 0}

    async def fake_thumb(session, token_mgr, item, size, min_width):
        await asyncio.sleep(0)               # 下載很快
        inflight["now"] += 1
        inflight["peak"] = max(inflight["peak"], inflight["now"])
        return b"x" * 1024

    def slow_write(path, data):
        time.sleep(0.005)                    # 寫檔很慢（NFS）
        inflight["now"] -= 1

    monkeypatch.setattr(jobs_module, "_download_thumbnail", fake_thumb)
    monkeypatch.setattr(jobs_module, "_write_file", slow_write)

    async def run():
        sem = asyncio.Semaphore(CONC)
        stats = {}
        await asyncio.gather(*[
            jobs_module._fetch_and_store(None, None, sem, {"id": f"f{i}"},
                                         f"/tmp/{i}", 2560, 1920, stats)
            for i in range(TOTAL)
        ])
        return stats

    stats = asyncio.run(run())

    assert stats["thumb"] == TOTAL
    # 關鍵斷言：尖峰未寫出張數不得超過並發上限。寫檔若搬到 sem 外，這裡會逼近 TOTAL。
    assert inflight["peak"] <= CONC, f"未寫出的圖片尖峰 {inflight['peak']} 超過上限 {CONC}"


def test_progress_commit_interval_is_not_tiny():
    """commit 是同步的（持鎖時不能 await），間隔太小會頻繁阻塞 event loop。"""
    assert jobs_module.PROGRESS_COMMIT_EVERY >= 100


# ── Spark 張數上限安全網 ─────────────────────────────────────────────────────

def _mk_photos(d, n, day="20260101"):
    for i in range(n):
        (d / f"{i:06d}_{day}_p{i}.jpg").write_bytes(b"x")


def test_sample_caps_at_target(tmp_path):
    """抽樣後張數不得超過目標——這是不會再撞 Spark 422 的保證。"""
    _mk_photos(tmp_path, 300)
    jobs_module._sample_by_day(str(tmp_path), 100)
    assert len(list(tmp_path.iterdir())) <= 100


def test_sample_is_noop_when_already_under_target(tmp_path):
    """已經不多於目標時不能再刪——續傳會重複呼叫，時長不可越續越短。"""
    _mk_photos(tmp_path, 50)
    jobs_module._sample_by_day(str(tmp_path), 100)
    assert len(list(tmp_path.iterdir())) == 50
    # 再跑一次仍不變
    jobs_module._sample_by_day(str(tmp_path), 100)
    assert len(list(tmp_path.iterdir())) == 50


def test_sample_spreads_across_days(tmp_path):
    """按天抽取：每天都要有幀入鏡，不能整段某天被吃掉。"""
    for day in ("20260101", "20260102", "20260103"):
        for i in range(100):
            (tmp_path / f"{day}_{i:04d}.jpg").write_bytes(b"x")
    jobs_module._sample_by_day(str(tmp_path), 30)
    remaining = [p.name for p in tmp_path.iterdir()]
    for day in ("20260101", "20260102", "20260103"):
        assert any(n.startswith(day) for n in remaining), f"{day} 整天被抽光"


def test_spark_max_images_default():
    """安全網的上限要與 Spark 實際限制一致（422: max is 50000）。"""
    assert jobs_module.SPARK_MAX_IMAGES == 50000


def test_concurrency_memory_budget_fits_container_limit():
    """sem 同時夾住下載與寫檔，所以並發數 x 單張大小就是在途緩衝的上限。

    容器 mem_limit 是 2 GiB，這裡確保並發數不會被調到讓緩衝吃掉整個額度。
    """
    per_image_mib = 2          # 實測縮圖 ~1 MB，抓 2 倍餘裕
    budget_mib = jobs_module.DOWNLOAD_CONCURRENCY * per_image_mib
    assert budget_mib <= 512, f"在途緩衝上限 {budget_mib} MiB 過大（容器上限 2 GiB）"


# ── 生成階段的顯示欄位 ───────────────────────────────────────────────────────

class _FakeSparkClient:
    """假裝成 httpx.AsyncClient，只回一份固定的 Spark 狀態。"""
    payload = {
        "status": "quality_gate",
        "percent_complete": 8,
        "estimated_time_remaining": "3h 27m",
        "current_stage": "processing",          # Spark 的內部英文狀態
        "stage_detail": "Batch 7/50 clip done: 598 keepers, 432 damaged",
        "image_count": 50000,
    }

    def __init__(self, *a, **kw): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def get(self, *a, **kw):
        class R:
            status_code = 200
            @staticmethod
            def json(): return _FakeSparkClient.payload
        return R()


def test_processing_stage_is_localised_and_keeps_spark_detail(
        gdrive_client, gdrive_db, make_user, auth_headers, monkeypatch):
    """生成階段：階段名要用我們的中文，Spark 的細節與 ETA 要往前端傳。

    回歸自 job 41：原本直接採用 Spark 的 current_stage，中文介面因此顯示英文
    "processing"；而真正有用的 stage_detail 與 estimated_time_remaining 被丟掉，
    使用者只看到一個不動的百分比，誤以為卡住。
    """
    user = make_user("stage1", "stage1@example.com", password="pw")
    job = _mk_job(gdrive_db, user, "processing", PARAMS, spark_job_id="spark-x")
    monkeypatch.setattr(jobs_module.httpx, "AsyncClient", _FakeSparkClient)

    d = gdrive_client.get(f"/jobs/gdrive/{job.id}", headers=auth_headers(user)).json()

    assert d["current_stage"] == "生成中"                     # 不是 "processing"
    assert d["stage_detail"] == _FakeSparkClient.payload["stage_detail"]
    assert d["estimated_time_remaining"] == "3h 27m"
    assert d["percent_complete"] == 8                          # 百分比仍以 Spark 為準
    assert d["spark_status"] == "quality_gate"


# ── 影片下載不得曝露 SPARK_API_KEY ───────────────────────────────────────────

def test_video_url_never_contains_spark_api_key(
        gdrive_client, gdrive_db, make_user, auth_headers, monkeypatch):
    """下載連結交給瀏覽器，裡面絕不能有 Spark 的 API key。

    回歸自 job 41：原本回傳 `{SPARK_API_URL}/jobs/{id}/download?api_key={KEY}`，
    等於把 Spark 的完整 API 權限交給任何拿到連結的人（分享、瀏覽器紀錄、Referer）。
    """
    monkeypatch.setattr(jobs_module, "SPARK_API_KEY", "super-secret-key")
    user = make_user("vid1", "vid1@example.com", password="pw")
    job = _mk_job(gdrive_db, user, "processing", PARAMS, spark_job_id="spark-v")
    monkeypatch.setattr(jobs_module.httpx, "AsyncClient", _FakeSparkClient)
    monkeypatch.setattr(_FakeSparkClient, "payload", {"status": "completed", "percent_complete": 100})

    detail = gdrive_client.get(f"/jobs/gdrive/{job.id}", headers=auth_headers(user)).json()
    listing = gdrive_client.get("/jobs/gdrive", headers=auth_headers(user)).json()

    url = detail["video_download_url"]
    assert url and "super-secret-key" not in url and "api_key" not in url
    assert f"/jobs/gdrive/{job.id}/video?t=" in url
    for row in listing:
        assert "super-secret-key" not in (row.get("video_download_url") or "")


def test_video_url_not_leaked_from_stored_column(
        gdrive_client, gdrive_db, make_user, auth_headers, monkeypatch):
    """舊資料的 video_url 欄位裡存著帶 key 的連結，不能再被吐出來。"""
    monkeypatch.setattr(jobs_module, "SPARK_API_KEY", "super-secret-key")
    user = make_user("vid2", "vid2@example.com", password="pw")
    job = _mk_job(gdrive_db, user, "completed", PARAMS, spark_job_id="spark-old",
                  video_url="https://spark.example/jobs/x/download?api_key=super-secret-key")

    listing = gdrive_client.get("/jobs/gdrive", headers=auth_headers(user)).json()

    assert all("super-secret-key" not in (r.get("video_download_url") or "") for r in listing)


def test_video_download_requires_valid_ticket(gdrive_client, gdrive_db, make_user):
    """沒有 ticket／偽造 ticket 都不能下載。"""
    user = make_user("vid3", "vid3@example.com", password="pw")
    job = _mk_job(gdrive_db, user, "completed", PARAMS, spark_job_id="spark-t")

    assert gdrive_client.get(f"/jobs/gdrive/{job.id}/video").status_code == 422  # 缺 t
    assert gdrive_client.get(f"/jobs/gdrive/{job.id}/video?t=garbage").status_code == 403


def test_video_ticket_is_bound_to_job_and_user(gdrive_client, gdrive_db, make_user):
    """ticket 綁 user+job：不能拿別支影片的 ticket 換這支，也不能用別人的。"""
    from auth import create_video_ticket
    owner = make_user("vid4a", "vid4a@example.com", password="pw")
    other = make_user("vid4b", "vid4b@example.com", password="pw")
    job_a = _mk_job(gdrive_db, owner, "completed", PARAMS, spark_job_id="spark-a")
    job_b = _mk_job(gdrive_db, owner, "completed", PARAMS, spark_job_id="spark-b")

    # 用 job_b 的 ticket 打 job_a
    t_b = create_video_ticket(owner.id, job_b.id)
    assert gdrive_client.get(f"/jobs/gdrive/{job_a.id}/video?t={t_b}").status_code == 403

    # 別人的 ticket（job 對得上，但 user 不是擁有者）→ 查不到這支 job
    t_other = create_video_ticket(other.id, job_a.id)
    assert gdrive_client.get(f"/jobs/gdrive/{job_a.id}/video?t={t_other}").status_code == 404


def test_video_ticket_purpose_is_isolated():
    """OAuth ticket 不能拿來下載影片，反之亦然（purpose 隔離）。"""
    from auth import create_gdrive_oauth_ticket, decode_video_ticket
    from auth import create_video_ticket, decode_gdrive_oauth_ticket
    assert decode_video_ticket(create_gdrive_oauth_ticket(15)) is None
    assert decode_gdrive_oauth_ticket(create_video_ticket(15, 41)) is None


# ── 續傳時覆寫設定 ───────────────────────────────────────────────────────────

def _capture_launch(monkeypatch):
    started = {}

    async def fake_pipeline(*a, **kw):
        started["kwargs"] = kw

    monkeypatch.setattr(jobs_module, "_run_gdrive_nas_pipeline", fake_pipeline)
    return started


def test_resume_applies_overrides(gdrive_client, gdrive_db, make_user,
                                  auth_headers, monkeypatch, _google_ok):
    """Spark 端因設定失敗時（如 job 42 的 quality gate 刷光整批導致死鎖），
    必須能換設定重送，否則只能原封重送再失敗一次，或重新下載數 GB。"""
    user = make_user("ov1", "ov1@example.com", password="pw")
    job = _mk_job(gdrive_db, user, "failed", PARAMS)
    started = _capture_launch(monkeypatch)

    r = gdrive_client.post(f"/jobs/gdrive/{job.id}/resume", headers=auth_headers(user),
                           json={"darkness_detection": False, "duration_seconds": 60})
    assert r.status_code == 200, r.text

    kw = started["kwargs"]
    assert kw["darkness"] is False                # 覆寫生效
    assert kw["duration_seconds"] == 60
    assert kw["rain_fog"] is True                 # 未指定的沿用原值
    assert kw["folder_ids"] == PARAMS["folder_ids"]
    assert set(r.json()["applied_overrides"]) == {"darkness_detection", "duration_seconds"}


def test_resume_overrides_are_persisted(gdrive_client, gdrive_db, make_user,
                                        auth_headers, monkeypatch, _google_ok):
    """覆寫要寫回 job_params：之後再被中斷時該沿用新設定，不能退回舊的。"""
    user = make_user("ov2", "ov2@example.com", password="pw")
    job = _mk_job(gdrive_db, user, "failed", PARAMS)
    _capture_launch(monkeypatch)

    gdrive_client.post(f"/jobs/gdrive/{job.id}/resume", headers=auth_headers(user),
                       json={"darkness_detection": False})

    gdrive_db.expire_all()
    saved = json.loads(gdrive_db.query(GDriveJob).get(job.id).job_params)
    assert saved["darkness_detection"] is False
    assert saved["rain_fog_detection"] is True    # 其餘不受影響


def test_resume_without_body_keeps_original_params(gdrive_client, gdrive_db, make_user,
                                                   auth_headers, monkeypatch, _google_ok):
    """不帶 body 的續傳（前端目前的用法）行為必須完全不變。"""
    user = make_user("ov3", "ov3@example.com", password="pw")
    job = _mk_job(gdrive_db, user, "interrupted", PARAMS)
    started = _capture_launch(monkeypatch)

    r = gdrive_client.post(f"/jobs/gdrive/{job.id}/resume", headers=auth_headers(user))
    assert r.status_code == 200

    kw = started["kwargs"]
    assert kw["darkness"] is False and kw["rain_fog"] is True   # 同 PARAMS
    assert kw["duration_seconds"] == 60
    assert r.json()["applied_overrides"] == {}


def test_resume_cannot_override_resolution(gdrive_client, gdrive_db, make_user,
                                           auth_headers, monkeypatch, _google_ok):
    """resolution 不開放覆寫：照片是依當初解析度抓對應尺寸的縮圖，
    事後改 4K 只會拿到不足尺寸的素材。"""
    user = make_user("ov4", "ov4@example.com", password="pw")
    job = _mk_job(gdrive_db, user, "failed", PARAMS)
    started = _capture_launch(monkeypatch)

    gdrive_client.post(f"/jobs/gdrive/{job.id}/resume", headers=auth_headers(user),
                       json={"resolution": "3840x2160"})

    assert started["kwargs"]["body_resolution"] == "1920x1080"   # 未被改動
