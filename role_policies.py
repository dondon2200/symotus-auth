"""角色權限政策：reseller／end_user 能不能做某個動作。

與 policies.py（被分享者依 camera_access 等級）是兩張表、兩種語意：
這裡管「角色 × 動作」，那裡管「分享等級 × 功能」。symotus_admin 不存列、一律放行。
目錄預設 = 2026-11-04 前寫死在各 router 的行為；seed 只補缺列、不覆寫管理員調過的值。
設計：symotus-frontend docs/superpowers/specs/2026-11-04-role-policies-design.md
"""
import time
from typing import Callable

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models import RolePolicy, User

ADMIN_ROLE = "symotus_admin"
ROLES = ("reseller", "end_user")
GROUPS = ("相機與專案", "帳號與邀請", "其他")

# (feature_key, group, description, {role: allowed})。順序＝管理頁顯示順序。
ROLE_FEATURE_DEFAULTS = [
    ("camera.create",           "相機與專案", "新增／配對相機",           {"reseller": True,  "end_user": False}),
    ("project.create",          "相機與專案", "建立專案",                 {"reseller": True,  "end_user": False}),
    ("project.delete",          "相機與專案", "刪除專案",                 {"reseller": True,  "end_user": False}),
    ("camera.delete.own",       "相機與專案", "刪除自己擁有的相機",       {"reseller": True,  "end_user": False}),
    ("camera.share.own",        "相機與專案", "分享自己擁有的相機",       {"reseller": True,  "end_user": False}),
    ("account.invite",          "帳號與邀請", "發帳號邀請連結",           {"reseller": True,  "end_user": False}),
    ("account.invite_reseller", "帳號與邀請", "發 reseller 邀請",         {"reseller": False, "end_user": False}),
    ("account.create",          "帳號與邀請", "直接建立帳號",             {"reseller": True,  "end_user": False}),
    ("account.grant_access",    "帳號與邀請", "直接授權相機給旗下帳號",   {"reseller": True,  "end_user": False}),
    ("invitation.revoke_any",   "帳號與邀請", "撤銷他人發的邀請",         {"reseller": False, "end_user": False}),
    ("jobs.view_all",           "其他",       "看全部縮時任務",           {"reseller": False, "end_user": False}),
    ("support.grant",           "其他",       "開技術支援授權給管理員",   {"reseller": True,  "end_user": False}),
]
CATALOGUE = {key: (group, desc, defaults) for key, group, desc, defaults in ROLE_FEATURE_DEFAULTS}


def description_of(feature_key: str) -> str:
    return CATALOGUE.get(feature_key, ("", feature_key, {}))[1]


def default_allowed(feature_key: str, role: str) -> bool:
    return bool(CATALOGUE.get(feature_key, ("", "", {}))[2].get(role, False))


# ── 60 秒 in-memory cache（每請求零額外 DB 查詢）────────────────
_cache: dict = {"at": 0.0, "rows": {}}
CACHE_TTL = 60


def get_role_policies(db: Session) -> dict:
    """{(feature_key, role): allowed}，60 秒內共用。"""
    now = time.time()
    if now - _cache["at"] > CACHE_TTL:
        _cache["rows"] = {(p.feature_key, p.role): bool(p.allowed) for p in db.query(RolePolicy).all()}
        _cache["at"] = now
    return _cache["rows"]


def invalidate_role_cache():
    _cache["at"] = 0.0


def role_allows(db: Session, feature_key: str, user: User) -> bool:
    """user 的角色能否做 feature_key。admin 一律 True；缺列回目錄預設；目錄沒有的鍵 False（fail-safe）。"""
    if user.role == ADMIN_ROLE:
        return True
    if feature_key not in CATALOGUE or user.role not in ROLES:
        return False
    hit = get_role_policies(db).get((feature_key, user.role))
    if hit is None:
        return default_allowed(feature_key, user.role)
    return hit


def require_feature(feature_key: str) -> Callable:
    """FastAPI dependency，取代 require_role(...)。不允許 → 403，訊息帶目錄說明。"""
    def checker(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> User:
        if not role_allows(db, feature_key, current_user):
            raise HTTPException(403, f"此角色沒有「{description_of(feature_key)}」的權限")
        return current_user
    return checker


def seed_role_policies(db: Session):
    """補齊缺少的列（不覆寫既有 allowed）。main.py startup 呼叫。"""
    existing = {(p.feature_key, p.role) for p in db.query(RolePolicy).all()}
    dirty = False
    for key, _group, _desc, defaults in ROLE_FEATURE_DEFAULTS:
        for role in ROLES:
            if (key, role) not in existing:
                db.add(RolePolicy(feature_key=key, role=role, allowed=defaults[role]))
                dirty = True
    if dirty:
        db.commit()
        invalidate_role_cache()
