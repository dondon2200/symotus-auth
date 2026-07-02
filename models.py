from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, ARRAY, UniqueConstraint
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

    # Camera Backend 帳號對應
    camera_email = Column(String, nullable=True)      # Camera Backend 對應帳號 email
    camera_user_id = Column(Integer, nullable=True)   # Camera Backend 的真實 user_id

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
    """相機存取授權"""
    """end_user 可以存取哪些相機（camera_id 對應現有後端的相機 ID）"""
    __tablename__ = "camera_access"
    # 0-c：同一 (相機, 用戶) 只應有一列，杜絕重複列導致「取消一列另一列仍通知」。
    # 註：僅對新建立的資料表生效；既有正式庫的重複列由執行期 update-all 邏輯容錯處理。
    __table_args__ = (UniqueConstraint("camera_id", "user_id", name="uq_camera_access_camera_user"),)

    id = Column(Integer, primary_key=True, index=True)
    camera_id = Column(Integer, nullable=False, index=True)  # 現有後端的 camera ID
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    granted_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    permission_level = Column(String, default="photos_stream", nullable=False)  # full / photos_stream / stream_only
    notify_on_online = Column(Boolean, default=True, nullable=False, server_default="true")  # 開機 LINE 通知
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
    # 接受邀請後賦予的角色（預設 end_user；僅 symotus_admin 發的邀請可指定 reseller）
    intended_role = Column(String, nullable=False, default="end_user")
    # reseller 邀請時預綁定的 Camera Backend 帳號（讓接受者一登入就能取 camera token、管理相機）
    camera_email = Column(String, nullable=True)
    camera_user_id = Column(Integer, nullable=True)
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
    video_url = Column(String, nullable=True)        # Spark 完成後的下載 URL
    error_message = Column(String, nullable=True)   # 失敗原因
    image_count = Column(Integer, nullable=True)
    processing_time_secs = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
# 注意：下面的欄位需要 ALTER TABLE 或在新環境自動建立
# TimelapsJob 額外欄位（已在 class 定義，這裡補充說明）

class GDriveJob(Base):
    """Google Drive 縮時影片任務（Auth Service 自己管理下載進度）"""
    __tablename__ = "gdrive_jobs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    folder_url = Column(String, nullable=True)              # 舊流程：公開分享連結（新流程不再使用）
    folder_id = Column(String, nullable=True)               # 新流程：Picker 選到的資料夾 id
    folder_name = Column(String, nullable=True)             # 資料夾顯示名稱（Picker 回傳）
    google_refresh_token = Column(String, nullable=True)    # 消費者 Drive refresh token（長任務續期下載用）
    status = Column(String, nullable=False, default="pending")
    # pending → downloading → uploading → processing → completed | failed
    total_images = Column(Integer, default=0)       # Drive 資料夾內圖片總數
    downloaded_count = Column(Integer, default=0)   # 已下載張數
    spark_job_id = Column(String, nullable=True)    # Spark 回傳的 job_id
    fps = Column(Integer, default=30)
    resolution = Column(String, nullable=True)
    video_url = Column(String, nullable=True)
    error_message = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CameraInvitation(Base):
    """相機存取邀請（連結式，點連結接受）"""
    __tablename__ = "camera_invitations"

    id = Column(Integer, primary_key=True, index=True)
    token = Column(String, unique=True, nullable=False, index=True)        # 分享連結 token
    inviter_id = Column(Integer, ForeignKey("users.id"), nullable=False)   # 邀請者
    invitee_id = Column(Integer, ForeignKey("users.id"), nullable=True)    # 接受者（接受後填入）
    camera_id = Column(Integer, nullable=False)
    camera_name = Column(String, nullable=True)                            # 方便顯示
    status = Column(String, default="pending")                             # pending / accepted / declined
    note = Column(String, nullable=True)
    permission_level = Column(String, default="photos_stream", nullable=False)  # full / photos_stream / stream_only
    expires_at = Column(DateTime, nullable=True)                           # None = 不過期
    created_at = Column(DateTime, default=datetime.utcnow)
    responded_at = Column(DateTime, nullable=True)
    is_public = Column(Boolean, default=False, nullable=False, server_default="false")  # 公開連結，不需登入
