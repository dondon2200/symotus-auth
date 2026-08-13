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


# ── seed_policies 一次性 remap（I3）────────────────────────────────
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import FeaturePolicy
from policies import seed_policies, FEATURE_DEFAULTS


def _sqlite_db():
    engine = create_engine("sqlite://")
    FeaturePolicy.__table__.create(engine)
    return sessionmaker(bind=engine)()


def test_seed_remap_runs_once_only():
    """已部署過三模式（photos.download 已在表中）＝非首次啟動：
    管理端事後把 camera.delete 鎖回 owner_only（恰為 remap 的舊預設），
    重啟 seed 不得把它還原成 full。"""
    db = _sqlite_db()
    for key, level, desc in FEATURE_DEFAULTS:
        db.add(FeaturePolicy(feature_key=key, min_level=level, description=desc, enabled=True))
    db.commit()
    db.query(FeaturePolicy).filter_by(feature_key="camera.delete").one().min_level = "owner_only"
    db.commit()
    seed_policies(db)
    assert db.query(FeaturePolicy).filter_by(feature_key="camera.delete").one().min_level == "owner_only"
    invalidate_cache()
    db.close()


def test_seed_remap_fires_on_first_migration():
    """舊版資料庫（無 photos.download，各列仍為舊預設）＝首次啟動：remap 生效。"""
    db = _sqlite_db()
    legacy = {"stream.view": "stream_only", "photos.view": "photos_stream",
              "notify.subscribe": "stream_only", "camera.delete": "owner_only",
              "device.replace": "owner_only"}
    for key, level in legacy.items():
        db.add(FeaturePolicy(feature_key=key, min_level=level, description="", enabled=True))
    db.commit()
    seed_policies(db)
    got = {p.feature_key: p.min_level for p in db.query(FeaturePolicy).all()}
    assert got["stream.view"] == "photos_stream"
    assert got["photos.view"] == "stream_only"
    assert got["notify.subscribe"] == "photos_stream"
    assert got["camera.delete"] == "full"
    assert got["device.replace"] == "full"
    assert "photos.download" in got  # 缺列已補齊
    invalidate_cache()
    db.close()
