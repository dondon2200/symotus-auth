"""R-015 照片打包下載：日期範圍、串流 zip、簽章網址與權限閘門。"""
import asyncio
import io
import time
import zipfile

import pytest
from fastapi import HTTPException

import routers.cameras as cameras_mod
import routers.public_camera as public_mod
from policies import invalidate_cache
from services.photo_archive import (
    ArchiveRangeError, EMPTY_NOTE_NAME, FAILED_LIST_NAME, MAX_DAYS, parse_range, stream_zip,
)


# ── 日期範圍 ────────────────────────────────────────────────────────

def test_單日與整月都回由舊到新的日期清單():
    assert parse_range("2026-11-24", "2026-11-24") == ["2026-11-24"]
    days = parse_range("2026-11-01", "2026-11-30")
    assert len(days) == 30 and days[0] == "2026-11-01" and days[-1] == "2026-11-30"


@pytest.mark.parametrize("start,end,code", [
    ("", "", "ARCHIVE_BAD_DATE"),
    ("2026/11/24", "2026-11-24", "ARCHIVE_BAD_DATE"),
    ("2026-11-24", "2026-11-23", "ARCHIVE_BAD_DATE"),
    ("2026-10-01", "2026-11-01", "ARCHIVE_RANGE_TOO_LONG"),
])
def test_不合法的範圍丟錯並帶錯誤碼(start, end, code):
    with pytest.raises(ArchiveRangeError) as e:
        parse_range(start, end)
    assert e.value.code == code


def test_剛好_31_天可以():
    assert len(parse_range("2026-10-01", "2026-10-31")) == MAX_DAYS


# ── 串流 zip ────────────────────────────────────────────────────────

def _collect(days, listing, photos):
    async def list_day(day):
        return listing.get(day, [])

    async def fetch_photo(path):
        return photos.get(path)

    async def run():
        return [c async for c in stream_zip(days, list_day, fetch_photo)]

    chunks = asyncio.run(run())
    return chunks, zipfile.ZipFile(io.BytesIO(b"".join(chunks)))


def test_照片依日期放進資料夾_內容原樣_分批送出():
    listing = {
        "2026-11-23": [{"name": f"2026_11_23_10_00_{i:02d}_000.jpg", "path": f"/h/S/2026-11-23/{i}.jpg"} for i in range(9)],
        "2026-11-24": [{"name": "2026_11_24_07_00_00_000.jpg", "path": "/h/S/2026-11-24/a.jpg"}],
    }
    photos = {f["path"]: f"data-{f['path']}".encode() for d in listing.values() for f in d}
    chunks, zf = _collect(["2026-11-23", "2026-11-24"], listing, photos)

    names = zf.namelist()
    assert len(names) == 10
    assert names[0] == "2026-11-23/2026_11_23_10_00_00_000.jpg"
    assert names[-1] == "2026-11-24/2026_11_24_07_00_00_000.jpg"
    assert zf.read("2026-11-24/2026_11_24_07_00_00_000.jpg") == b"data-/h/S/2026-11-24/a.jpg"
    assert zf.testzip() is None
    # 不壓縮；檔案時間取自檔名
    info = zf.getinfo("2026-11-24/2026_11_24_07_00_00_000.jpg")
    assert info.compress_type == zipfile.ZIP_STORED
    assert info.date_time == (2026, 11, 24, 7, 0, 0)
    # 9 張分 3 批（每批 4 張）＋第二天 1 批＋結尾：不是整包一次吐出來
    assert len(chunks) >= 4


def test_抓不到的照片記進失敗清單_不中斷整包():
    listing = {"2026-11-24": [
        {"name": "a.jpg", "path": "/h/S/2026-11-24/a.jpg"},
        {"name": "b.jpg", "path": "/h/S/2026-11-24/b.jpg"},
    ]}
    _, zf = _collect(["2026-11-24"], listing, {"/h/S/2026-11-24/a.jpg": b"A"})
    assert zf.namelist() == ["2026-11-24/a.jpg", FAILED_LIST_NAME]
    assert "2026-11-24/b.jpg" in zf.read(FAILED_LIST_NAME).decode("utf-8")


def test_沒有照片時壓縮檔裡放一份說明():
    _, zf = _collect(["2026-11-24"], {}, {})
    assert zf.namelist() == [EMPTY_NOTE_NAME]


def test_檔名不能帶路徑進壓縮檔():
    listing = {"2026-11-24": [{"name": "../../etc/passwd", "path": "/h/S/2026-11-24/x.jpg"}]}
    _, zf = _collect(["2026-11-24"], listing, {"/h/S/2026-11-24/x.jpg": b"X"})
    assert zf.namelist() == ["2026-11-24/passwd"]


# ── 簽章網址 ────────────────────────────────────────────────────────

class FakeDb:
    def query(self, model):
        raise AssertionError("簽章不過就不該碰資料庫")


def _download(**over):
    args = dict(camera_id=7, start="2026-11-24", end="2026-11-24", uid=11, exp=int(time.time()) + 60)
    args.update(over)
    sig = over.get("sig", public_mod.photo_archive_sig(args["camera_id"], args["uid"], args["start"], args["end"], args["exp"]))
    return public_mod.download_photo_archive(
        camera_id=args["camera_id"], start=args["start"], end=args["end"], uid=args["uid"],
        exp=args["exp"], sig=sig, db=FakeDb(),
    )


def test_簽章綁定相機_使用者_日期與期限():
    exp = int(time.time()) + 60
    base = public_mod.photo_archive_sig(7, 11, "2026-11-24", "2026-11-24", exp)
    assert base != public_mod.photo_archive_sig(8, 11, "2026-11-24", "2026-11-24", exp)
    assert base != public_mod.photo_archive_sig(7, 12, "2026-11-24", "2026-11-24", exp)
    assert base != public_mod.photo_archive_sig(7, 11, "2026-11-01", "2026-11-24", exp)
    assert base != public_mod.photo_archive_sig(7, 11, "2026-11-24", "2026-11-24", exp + 1)


def test_沒有簽章或簽章不符回_403():
    for sig in ("", "0" * 64):
        with pytest.raises(HTTPException) as e:
            asyncio.run(_download(sig=sig))
        assert e.value.status_code == 403
        assert e.value.detail["code"] == "ARCHIVE_LINK_EXPIRED"


def test_過期的網址回_403():
    with pytest.raises(HTTPException) as e:
        asyncio.run(_download(exp=int(time.time()) - 1))
    assert e.value.status_code == 403


def test_下載路徑不會被公開分享的_token_路由接走():
    import os
    os.environ.setdefault("CAMERA_SERVICE_KEY", "x")
    from main import app
    from starlette.routing import Match
    scope = {"type": "http", "method": "GET", "path": "/cameras/public/photo-archive/zip"}
    first = next(r for r in app.routes if r.matches(scope)[0] == Match.FULL)
    assert first.endpoint is public_mod.download_photo_archive


# ── 取得下載網址的權限閘門 ────────────────────────────────────────────

class FakeUser:
    id = 11
    role = "end_user"
    camera_email = None
    is_active = True


class FakeAccess:
    def __init__(self, level):
        self.camera_id = 7
        self.user_id = 11
        self.granted_by = 99
        self.permission_level = level


class FakeQuery:
    def __init__(self, rows): self._rows = rows
    def filter(self, *a, **k): return self
    def all(self): return self._rows
    def first(self): return self._rows[0] if self._rows else None


class AccessDb:
    def __init__(self, level): self._level = level
    def query(self, model):
        if getattr(model, "__name__", "") == "CameraAccess":
            return FakeQuery([FakeAccess(self._level)] if self._level else [])
        return FakeQuery([])


class FakeRequest:
    def __init__(self, body): self._body = body
    async def json(self): return self._body


@pytest.fixture()
def no_network(monkeypatch):
    invalidate_cache()
    async def _tok(user): return "tok"
    monkeypatch.setattr(cameras_mod, "get_camera_backend_token", _tok)
    monkeypatch.setattr(cameras_mod, "assert_camera_unlocked", lambda db, user, cid: None)
    async def _serial(tok, cid): return "SN1"
    monkeypatch.setattr(cameras_mod, "camera_serial_for_archive", _serial)

    class _Resp:
        status_code = 200
    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): return _Resp()
    monkeypatch.setattr(cameras_mod.httpx, "AsyncClient", _Client)


def _create(db, body):
    return asyncio.run(cameras_mod.create_photo_archive_link(
        camera_id=7, request=FakeRequest(body), current_user=FakeUser(), db=db))


def test_有下載權限的人拿到簽章網址(no_network):
    out = _create(AccessDb("photos_stream"), {"start_date": "2026-11-01", "end_date": "2026-11-30"})
    assert out["days"] == 30 and out["expires_in"] == 300
    assert out["url"].startswith("/cameras/public/photo-archive/zip?camera_id=7&start=2026-11-01&end=2026-11-30&uid=11&exp=")
    assert "&sig=" in out["url"]


def test_僅串流權限的被分享者拿不到(no_network):
    with pytest.raises(HTTPException) as e:
        _create(AccessDb("stream_only"), {"start_date": "2026-11-24", "end_date": "2026-11-24"})
    assert e.value.status_code == 403
    assert "photos.download" in e.value.detail


def test_沒有這台相機權限的人拿不到(no_network):
    with pytest.raises(HTTPException) as e:
        _create(AccessDb(None), {"start_date": "2026-11-24", "end_date": "2026-11-24"})
    assert e.value.status_code == 403
    assert e.value.detail["code"] == "CAMERA_FORBIDDEN"


def test_超過_31_天回_400(no_network):
    with pytest.raises(HTTPException) as e:
        _create(AccessDb("full"), {"start_date": "2026-10-01", "end_date": "2026-11-30"})
    assert e.value.status_code == 400
    assert e.value.detail["code"] == "ARCHIVE_RANGE_TOO_LONG"


def test_同時打包數已滿時取連結就回_429(no_network, monkeypatch):
    monkeypatch.setattr(public_mod, "_photo_archive_active", public_mod.PHOTO_ARCHIVE_MAX_CONCURRENT)
    with pytest.raises(HTTPException) as e:
        _create(AccessDb("full"), {"start_date": "2026-11-24", "end_date": "2026-11-24"})
    assert e.value.status_code == 429
    assert e.value.detail["code"] == "ARCHIVE_BUSY"
