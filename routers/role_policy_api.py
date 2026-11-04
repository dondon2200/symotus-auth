"""角色權限政策的 HTTP 端點：管理員讀寫在 /admin/role-policies*，任何登入者讀自己的在 /role-policies/me。
設計：symotus-frontend docs/superpowers/specs/2026-11-04-role-policies-design.md"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from audit import log_action
from auth import get_current_user, require_role
from database import get_db
from models import RolePolicy, User
from role_policies import (ADMIN_ROLE, CATALOGUE, ROLE_FEATURE_DEFAULTS, ROLES,
                           default_allowed, invalidate_role_cache, role_allows)

router = APIRouter(tags=["role_policies"])


def _serialize(rows: dict) -> list[dict]:
    out = []
    for key, group, desc, defaults in ROLE_FEATURE_DEFAULTS:
        roles, updated_at = {}, None
        for role in ROLES:
            p = rows.get((key, role))
            allowed = bool(p.allowed) if p else defaults[role]
            roles[role] = {"allowed": allowed, "is_default": allowed == defaults[role]}
            if p and p.updated_at and (updated_at is None or p.updated_at > updated_at):
                updated_at = p.updated_at
        out.append({"feature_key": key, "group": group, "description": desc, "roles": roles,
                    "defaults": dict(defaults), "updated_at": updated_at.isoformat() if updated_at else None})
    return out


@router.get("/admin/role-policies")
def list_role_policies(db: Session = Depends(get_db), _=Depends(require_role(ADMIN_ROLE))):
    """依目錄順序回 12 列；缺列用目錄預設補，前端不用管表有沒有 seed。"""
    rows = {(p.feature_key, p.role): p for p in db.query(RolePolicy).all()}
    return _serialize(rows)


@router.put("/admin/role-policies/{feature_key}/{role}")
def update_role_policy(feature_key: str, role: str, body: dict, db: Session = Depends(get_db),
                       current_user: User = Depends(require_role(ADMIN_ROLE))):
    if feature_key not in CATALOGUE:
        raise HTTPException(404, "feature_key 不在目錄裡")
    if role not in ROLES:
        raise HTTPException(400, f"role 僅能是 {'/'.join(ROLES)}")
    allowed = body.get("allowed")
    if not isinstance(allowed, bool):
        raise HTTPException(400, "allowed 必須是布林值")
    p = db.query(RolePolicy).filter(RolePolicy.feature_key == feature_key, RolePolicy.role == role).first()
    if p is None:
        p = RolePolicy(feature_key=feature_key, role=role, allowed=allowed)
        db.add(p)
    p.allowed = allowed
    p.updated_by = current_user.id
    p.updated_at = datetime.utcnow()
    db.flush()
    log_action(db, current_user, "update_role_policy", "role_policy", p.id, f"{feature_key}/{role}={allowed}")
    db.commit()
    invalidate_role_cache()
    return {"feature_key": feature_key, "role": role, "allowed": allowed,
            "is_default": allowed == default_allowed(feature_key, role)}


@router.post("/admin/role-policies/reset")
def reset_role_policies(db: Session = Depends(get_db), current_user: User = Depends(require_role(ADMIN_ROLE))):
    """一鍵還原目錄預設（防止改壞把功能鎖死）。"""
    rows = {(p.feature_key, p.role): p for p in db.query(RolePolicy).all()}
    for key, _g, _d, defaults in ROLE_FEATURE_DEFAULTS:
        for role in ROLES:
            p = rows.get((key, role))
            if p is None:
                db.add(RolePolicy(feature_key=key, role=role, allowed=defaults[role], updated_by=current_user.id))
            else:
                p.allowed = defaults[role]
                p.updated_by = current_user.id
    log_action(db, current_user, "reset_role_policies", "role_policy", None, "restore all defaults")
    db.commit()
    invalidate_role_cache()
    return {"ok": True}


@router.get("/role-policies/me")
def my_role_policies(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """前端藏按鈕用；安全判斷仍在各端點。admin 回全部鍵。"""
    allowed = [key for key, _g, _d, _df in ROLE_FEATURE_DEFAULTS if role_allows(db, key, current_user)]
    return {"role": current_user.role, "allowed": allowed}
