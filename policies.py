"""功能權限政策：目錄（種子預設＝原硬編碼行為）、60 秒 cache、等級比較、寫入路徑分流。

適用對象：透過 camera_access 存取相機的「被分享者」。
擁有者（無 access 列、憑自己 camera token）與 symotus_admin 不受政策限制。
"""
import time
from typing import Optional
from sqlalchemy.orm import Session
from models import FeaturePolicy

# 等級序：數字越大權限越高；owner_only 表示被分享者一律不可（僅擁有者/admin）
LEVEL_ORDER = {"stream_only": 0, "photos_stream": 1, "full": 2, "owner_only": 3}

# (feature_key, 預設 min_level, 說明)
FEATURE_DEFAULTS = [
    ("stream.view",      "stream_only",   "即時串流觀看"),
    ("photos.view",      "photos_stream", "照片瀏覽/相簿/預覽縮時"),
    ("timelapse.create", "photos_stream", "縮時影片產生"),
    ("camera.settings",  "full",          "相機設定/排程寫入（image/osd/timesnap/timer…）"),
    ("camera.control",   "full",          "即時控制（PTZ/重啟/自動對焦）"),
    ("camera.rename",    "full",          "相機改名"),
    ("camera.share",     "full",          "發相機分享邀請"),
    ("camera.unbind",    "full",          "解除綁定"),
    ("camera.delete",    "owner_only",    "刪除相機/裝置更換"),
    ("notify.subscribe", "stream_only",   "LINE 開機通知訂閱"),
]

# 通用 proxy 寫入路徑 → feature_key（首段比對）
_CONTROL_PREFIXES = ("ptz", "reboot", "restart", "autofocus", "focus")
_TIMELAPSE_PREFIXES = ("timelapse-jobs", "prepare-timelapse")


def seed_policies(db: Session):
    """補齊缺少的政策列（不覆蓋既有調整值）。"""
    existing = {p.feature_key for p in db.query(FeaturePolicy).all()}
    added = False
    for key, level, desc in FEATURE_DEFAULTS:
        if key not in existing:
            db.add(FeaturePolicy(feature_key=key, min_level=level, description=desc, enabled=True))
            added = True
    if added:
        db.commit()


# ── 60 秒 in-memory cache（每請求零額外 DB 查詢）─────────────────
_cache: dict = {"at": 0.0, "policies": {}}
CACHE_TTL = 60


def get_policies(db: Session) -> dict:
    """回傳 {feature_key: FeaturePolicy 快照 dict}，60 秒內共用。"""
    now = time.time()
    if now - _cache["at"] > CACHE_TTL:
        rows = db.query(FeaturePolicy).all()
        _cache["policies"] = {
            p.feature_key: {"min_level": p.min_level, "enabled": p.enabled,
                            "description": p.description}
            for p in rows
        }
        _cache["at"] = now
    return _cache["policies"]


def invalidate_cache():
    _cache["at"] = 0.0


def level_allows(db: Session, feature_key: str, user_level: Optional[str]) -> bool:
    """被分享者以 user_level 能否使用 feature_key。政策缺列時 fail-safe 回退預設。"""
    pol = get_policies(db).get(feature_key)
    if pol is None:
        default = next((lv for k, lv, _ in FEATURE_DEFAULTS if k == feature_key), "full")
        pol = {"min_level": default, "enabled": True}
    if not pol["enabled"]:
        return False
    need = LEVEL_ORDER.get(pol["min_level"], 2)
    have = LEVEL_ORDER.get(user_level or "photos_stream", 1)
    if need >= LEVEL_ORDER["owner_only"]:
        return False  # owner_only：被分享者一律不可
    return have >= need


def feature_for_write(path: str) -> str:
    """通用 proxy 寫入請求的路徑 → feature_key。path 為 /cameras/{id}/ 之後的部份（可為空=改名）。"""
    first = (path or "").split("/")[0].lower()
    if not first:
        return "camera.rename"
    if first.startswith(_CONTROL_PREFIXES):
        return "camera.control"
    if first.startswith(_TIMELAPSE_PREFIXES):
        # 產縮時屬「使用照片」而非「改設定」：photos_stream 即可（原 F-5 一律要 full，
        # 與 UI 開放 photos_stream 產縮時矛盾——政策化後在此修正）
        return "timelapse.create"
    return "camera.settings"
