from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, ARRAY
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship, declarative_base
from datetime import datetime
import uuid

Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    full_name = Column(String, nullable=True)
    hashed_password = Column(String, nullable=True)  # nullable for OAuth-only users
    role = Column(String, nullable=False, default="reseller")  # symotus_admin | reseller | end_user
    is_active = Column(Boolean, default=True)
    reseller_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # end_user -> reseller
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    # OAuth
    google_id = Column(String, nullable=True, unique=True)
    line_id = Column(String, nullable=True, unique=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    camera_accesses = relationship("CameraAccess", foreign_keys="CameraAccess.user_id", back_populates="user")
    granted_accesses = relationship("CameraAccess", foreign_keys="CameraAccess.granted_by", back_populates="granter")
    sent_invites = relationship("InviteToken", foreign_keys="InviteToken.reseller_id", back_populates="reseller")


class CameraAccess(Base):
    """end_user 可以存取哪些相機（camera_id 對應現有後端的相機 ID）"""
    __tablename__ = "camera_access"

    id = Column(Integer, primary_key=True, index=True)
    camera_id = Column(Integer, nullable=False, index=True)  # 現有後端的 camera ID
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    granted_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", foreign_keys=[user_id], back_populates="camera_accesses")
    granter = relationship("User", foreign_keys=[granted_by], back_populates="granted_accesses")


class InviteToken(Base):
    """二房東發出的邀請連結"""
    __tablename__ = "invite_tokens"

    id = Column(Integer, primary_key=True, index=True)
    token = Column(String, unique=True, nullable=False, default=lambda: str(uuid.uuid4()))
    reseller_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    camera_ids = Column(ARRAY(Integer), nullable=True)  # 邀請時預先分配的相機，null = 不預分配
    email = Column(String, nullable=True)  # 限定 email，null = 任何人都可以用
    status = Column(String, nullable=False, default="pending")  # pending | accepted | expired | revoked
    expires_at = Column(DateTime, nullable=False)
    accepted_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    accepted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    reseller = relationship("User", foreign_keys=[reseller_id], back_populates="sent_invites")


class TechSupportGrant(Base):
    """二房東授權 Symotus 技術支援（48小時）"""
    __tablename__ = "tech_support_grants"

    id = Column(Integer, primary_key=True, index=True)
    reseller_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    granted_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    camera_ids = Column(ARRAY(Integer), nullable=True)  # null = 該 reseller 全部相機
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class RefreshToken(Base):
    """JWT refresh token 管理"""
    __tablename__ = "refresh_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    token = Column(String, unique=True, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    revoked = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class TimelapsJob(Base):
    """縮時影片任務記錄，跨裝置同步"""
    __tablename__ = "timelapse_jobs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    job_id = Column(String, nullable=False, unique=True)  # Spark 的 job_id
    camera_id = Column(Integer, nullable=True)
    camera_name = Column(String, nullable=True)
    serial_id = Column(String, nullable=True)
    status = Column(String, nullable=False, default="processing")  # processing | completed | failed
    percent_complete = Column(Integer, default=0)
    start_date = Column(String, nullable=True)
    end_date = Column(String, nullable=True)
    fps = Column(Integer, nullable=True)
    resolution = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
