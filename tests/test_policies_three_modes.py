"""三模式政策等級（spec D3/D5/D6）：
stream.view/notify.subscribe 升為 photos_stream、photos.view 降為 stream_only、
新增 photos.download、camera.delete/device.replace 開放 full。
測試走 FEATURE_DEFAULTS fallback（FakeDb 回空政策表）。"""
import policies
from policies import level_allows, invalidate_cache


class _Q:
    def all(self):
        return []


class _Db:
    def query(self, model):
        return _Q()


def setup_function(_):
    invalidate_cache()


def test_stream_view_requires_photos_stream():
    db = _Db()
    assert not level_allows(db, "stream.view", "stream_only")
    assert level_allows(db, "stream.view", "photos_stream")


def test_photos_view_open_to_stream_only():
    assert level_allows(_Db(), "photos.view", "stream_only")


def test_photos_download_requires_photos_stream():
    db = _Db()
    assert not level_allows(db, "photos.download", "stream_only")
    assert level_allows(db, "photos.download", "photos_stream")


def test_notify_subscribe_requires_photos_stream():
    db = _Db()
    assert not level_allows(db, "notify.subscribe", "stream_only")
    assert level_allows(db, "notify.subscribe", "photos_stream")


def test_delete_and_replace_open_to_full():
    db = _Db()
    assert level_allows(db, "camera.delete", "full")
    assert level_allows(db, "device.replace", "full")
    assert not level_allows(db, "camera.delete", "photos_stream")
