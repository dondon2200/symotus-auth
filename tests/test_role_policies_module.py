"""角色權限政策模組：目錄預設＝現行行為、admin 一律放行、缺列回預設、翻轉、快取失效、seed 不覆寫。
設計：symotus-frontend docs/superpowers/specs/2026-11-04-role-policies-design.md"""
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import RolePolicy
from role_policies import (ROLE_FEATURE_DEFAULTS, ROLES, role_allows, require_feature,
                           seed_role_policies, invalidate_role_cache)


class _User:
    def __init__(self, role, id=1):
        self.role, self.id = role, id


class _Row:
    def __init__(self, key, role, allowed):
        self.feature_key, self.role, self.allowed = key, role, allowed


class _Q:
    def __init__(self, rows): self._rows = rows
    def all(self): return self._rows


class FakeDb:
    def __init__(self, rows=()): self.rows = list(rows)
    def query(self, model): return _Q(self.rows if model is RolePolicy else [])


def setup_function(_):
    invalidate_role_cache()


def test_defaults_match_hardcoded_behaviour():
    db = FakeDb()
    for key, _g, _d, defaults in ROLE_FEATURE_DEFAULTS:
        for role in ROLES:
            assert role_allows(db, key, _User(role)) is defaults[role], (key, role)


def test_catalogue_has_twelve_keys_in_three_groups():
    assert len(ROLE_FEATURE_DEFAULTS) == 12
    assert {g for _k, g, _d, _df in ROLE_FEATURE_DEFAULTS} == {"相機與專案", "帳號與邀請", "其他"}
    assert not any(d["end_user"] for _k, _g, _d, d in ROLE_FEATURE_DEFAULTS)  # end_user 預設全關


def test_admin_always_allowed_even_when_rows_say_no():
    db = FakeDb([_Row("camera.create", "reseller", False)])
    assert role_allows(db, "camera.create", _User("symotus_admin"))
    assert role_allows(db, "not.a.key", _User("symotus_admin"))


def test_row_overrides_default():
    db = FakeDb([_Row("camera.create", "end_user", True), _Row("camera.create", "reseller", False)])
    assert role_allows(db, "camera.create", _User("end_user"))
    assert not role_allows(db, "camera.create", _User("reseller"))


def test_unknown_key_is_denied_for_non_admin():
    assert not role_allows(FakeDb(), "not.a.key", _User("reseller"))


def test_cache_until_invalidated():
    db = FakeDb()
    assert not role_allows(db, "camera.create", _User("end_user"))
    db.rows.append(_Row("camera.create", "end_user", True))
    assert not role_allows(db, "camera.create", _User("end_user"))  # 仍是快取
    invalidate_role_cache()
    assert role_allows(db, "camera.create", _User("end_user"))


def test_require_feature_403_with_description():
    checker = require_feature("camera.create")
    with pytest.raises(HTTPException) as e:
        checker(current_user=_User("end_user"), db=FakeDb())
    assert e.value.status_code == 403
    assert "新增／配對相機" in e.value.detail
    assert checker(current_user=_User("reseller"), db=FakeDb()).role == "reseller"


def _sqlite_db():
    engine = create_engine("sqlite://")
    RolePolicy.__table__.create(engine)
    return sessionmaker(bind=engine)()


def test_seed_fills_all_rows_once_and_keeps_adjusted_values():
    db = _sqlite_db()
    seed_role_policies(db)
    assert db.query(RolePolicy).count() == len(ROLE_FEATURE_DEFAULTS) * len(ROLES)
    row = db.query(RolePolicy).filter_by(feature_key="camera.create", role="end_user").one()
    row.allowed = True
    db.commit()
    seed_role_policies(db)
    assert db.query(RolePolicy).count() == len(ROLE_FEATURE_DEFAULTS) * len(ROLES)
    assert db.query(RolePolicy).filter_by(feature_key="camera.create", role="end_user").one().allowed is True
    db.close()
