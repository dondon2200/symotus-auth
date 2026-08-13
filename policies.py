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
# 2026-08 三模式改版（spec 2026-08-13-camera-share-three-modes-design.md，D3/D5/D6）：
# 僅預覽觀看(stream_only)＝縮時預覽＋相簿檢視，無串流/通知/下載；full 可刪機/換機。
FEATURE_DEFAULTS = [
    ("stream.view",      "photos_stream", "即時串流觀看"),
    ("photos.view",      "stream_only",   "照片瀏覽/相簿/預覽縮時"),
    ("photos.download",  "photos_stream", "下載照片原圖/縮時影片"),
    ("timelapse.create", "photos_stream", "縮時影片產生"),
    ("camera.settings",  "full",          "相機設定/排程寫入（image/osd/timesnap/timer…）"),
    ("camera.control",   "full",          "即時控制（PTZ/重啟/自動對焦）"),
    ("camera.rename",    "full",          "相機改名"),
    ("camera.share",     "full",          "發相機分享邀請"),
    ("camera.unbind",    "full",          "解除綁定"),
    ("camera.delete",    "full",          "刪除相機"),
    ("device.replace",   "full",          "裝置更換（換機/撤下裝置）"),
    ("notify.subscribe", "photos_stream", "LINE 開機通知訂閱"),
]

# 三模式改版的一次性 remap：僅在「新程式碼首次啟動」（見 seed_policies 的
# first_migration 判定）執行；既有環境政策列仍等於「舊預設」才改為新預設，
# 管理端調過的值不動。(feature_key, 舊預設, 新預設)
_LEVEL_MIGRATIONS = [
    ("stream.view",      "stream_only",   "photos_stream"),
    ("photos.view",      "photos_stream", "stream_only"),
    ("notify.subscribe", "stream_only",   "photos_stream"),
    ("camera.delete",    "owner_only",    "full"),
    ("device.replace",   "owner_only",    "full"),
]

# 通用 proxy 寫入路徑 → feature_key（首段比對）
_CONTROL_PREFIXES = ("ptz", "reboot", "restart", "autofocus", "focus")
_TIMELAPSE_PREFIXES = ("timelapse-jobs", "prepare-timelapse")
_DEVICE_PREFIXES = ("replace-device", "release-device")


def seed_policies(db: Session):
    """補齊缺少的政策列（不覆蓋既有 min_level/enabled 調整值）。

    description 為目錄文案（管理端不可編輯），目錄改字時就地同步——例如
    camera.delete 由「刪除相機/裝置更換」拆分後只剩「刪除相機」。
    """
    rows = {p.feature_key: p for p in db.query(FeaturePolicy).all()}
    # 一次性 remap 的標記：photos.download 只在三模式改版後才存在，缺列＝
    # 新程式碼「首次」啟動（首度部署三模式）。之後每次重啟不得再跑 remap，
    # 否則管理端事後的調整（例如把 camera.delete 重新鎖回 owner_only，恰等於
    # remap 的「舊預設」）會在每次重啟被靜默還原成新預設。
    first_migration = "photos.download" not in rows
    dirty = False
    for key, level, desc in FEATURE_DEFAULTS:
        p = rows.get(key)
        if p is None:
            db.add(FeaturePolicy(feature_key=key, min_level=level, description=desc, enabled=True))
            dirty = True
        elif p.description != desc:
            p.description = desc
            dirty = True
    if first_migration:
        for key, old_level, new_level in _LEVEL_MIGRATIONS:
            p = rows.get(key)
            if p is not None and p.min_level == old_level:
                p.min_level = new_level
                dirty = True
    if dirty:
        db.commit()
        invalidate_cache()


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
    if first.startswith(_DEVICE_PREFIXES):
        # 換機/撤下裝置：與「刪除相機」拆開獨立控管（原先落在 camera.settings）
        return "device.replace"
    if first.startswith(_TIMELAPSE_PREFIXES):
        # 產縮時屬「使用照片」而非「改設定」：photos_stream 即可（原 F-5 一律要 full，
        # 與 UI 開放 photos_stream 產縮時矛盾——政策化後在此修正）
        return "timelapse.create"
    return "camera.settings"
