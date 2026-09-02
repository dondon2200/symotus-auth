"""相機分享自助建帳（spec 2026-09-02 S1-S5）。sqlite in-memory 只建需要的表。"""
import asyncio
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import (
    Base, User, CameraInvitation, CameraAccess, AuditLog, FeaturePolicy, RefreshToken,
)
from policies import invalidate_cache


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[
        User.__table__, CameraInvitation.__table__, CameraAccess.__table__,
        AuditLog.__table__, FeaturePolicy.__table__, RefreshToken.__table__,
    ])
    invalidate_cache()   # 政策表為空 → level_allows 走 FEATURE_DEFAULTS fallback
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    invalidate_cache()


@pytest.fixture()
def reseller(db):
    u = User(id=1, username="rs", email="rs@x.com", role="reseller")
    db.add(u); db.commit()
    return u


def test_new_columns_have_expected_defaults(db, reseller):
    inv = CameraInvitation(token="tok1", inviter_id=reseller.id, camera_id=7,
                           permission_level="photos_stream")
    db.add(inv); db.commit(); db.refresh(inv)
    assert inv.invitee_email is None
    assert inv.signup_limit is None      # NULL 由 _signup_limit() 視為 10
    assert inv.signup_count == 0
