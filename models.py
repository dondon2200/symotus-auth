from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, ForeignKey, ARRAY, UniqueConstraint, Float, Index, text
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
    invitation_id = Column(Integer, nullable=True, index=True)  # 來源邀請（D2）；舊資料為 NULL，撤銷時 fallback 比對 granted_by+permission_level
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
    # pending → listing → downloading → submitted → processing → completed | failed
    # 另有 interrupted：服務重啟／部署導致下載中斷，NAS 上的檔案仍在，可續傳
    total_images = Column(Integer, default=0)       # Drive 資料夾內圖片總數
    downloaded_count = Column(Integer, default=0)   # 已下載張數
    # 續傳用：重建下載清單所需的原始參數（JSON），沒有它就無法在重啟後接續
    job_params = Column(Text, nullable=True)
    spark_job_id = Column(String, nullable=True)    # Spark 回傳的 job_id
    fps = Column(Integer, default=30)
    resolution = Column(String, nullable=True)
    video_url = Column(String, nullable=True)
    error_message = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class GoogleDriveCredential(Base):
    """使用者長期綁定的 Google Drive 憑證（redirect flow 用）。

    一位使用者一組。refresh_token 讓後端能隨時換發 access token：
    給 Picker 用、也給下載 pipeline 用，因此前端不必再自己跑 OAuth 彈窗。
    """
    __tablename__ = "google_drive_credentials"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False, index=True)
    refresh_token = Column(String, nullable=False)
    google_email = Column(String, nullable=True)     # 顯示用：「已連接 xxx@gmail.com」
    scope = Column(String, nullable=True)            # 實際取得的 scope
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class FeaturePolicy(Base):
    """功能權限政策：被分享者（camera_access）依授權等級可用的功能。
    單一事實來源；種子預設 = 原硬編碼行為。admin 角色不受政策限制。"""
    __tablename__ = "feature_policies"

    id = Column(Integer, primary_key=True, index=True)
    feature_key = Column(String, unique=True, nullable=False, index=True)  # 例 camera.control
    # 最低需求等級：stream_only < photos_stream < full < owner_only（owner_only=被分享者一律不可）
    min_level = Column(String, nullable=False, default="full")
    enabled = Column(Boolean, nullable=False, default=True)  # false = 功能對被分享者全面停用
    description = Column(String, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = Column(Integer, nullable=True)


class AuditLog(Base):
    """帳號/授權管理操作稽核（誰、何時、對誰做了什麼）"""
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    actor_id = Column(Integer, nullable=True)          # 操作者 user id；service key 操作為 None
    actor_username = Column(String, nullable=True)     # 快照，避免帳號改名/刪除後無法追溯
    action = Column(String, nullable=False, index=True)  # e.g. update_user / grant_access / revoke_invitation
    target_type = Column(String, nullable=True)        # user / camera_access / invitation / invite_token / support_grant
    target_id = Column(Integer, nullable=True)
    detail = Column(String, nullable=True)             # 精簡描述（變更欄位、相機 id 等）
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


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


# ── Billing 計費模組 ─────────────────────────────────────────────
# 設計見 symotus-frontend/docs/superpowers/specs/2026-08-20-billing-rebuild-design.md
# 金額一律整數 TWD；發票明細存快照（方案改價/相機改名不得回頭改變已開發票）。

class BillingPlan(Base):
    """計費方案"""
    __tablename__ = "billing_plans"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    monthly_fee = Column(Integer, nullable=False, default=0)         # TWD/月
    timelapse_quota_secs = Column(Integer, nullable=False, default=0)  # 0 = 不限
    storage_quota_gb = Column(Integer, nullable=False, default=0)      # 0 = 不限
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class BillingCustomer(Base):
    """客戶的計費設定。與 users 一對一，但不污染 User 表。"""
    __tablename__ = "billing_customers"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    billing_day = Column(Integer, nullable=False, default=1)  # 1-28，避開月底不存在的日期
    frozen = Column(Boolean, nullable=False, default=False)
    frozen_at = Column(DateTime, nullable=True)
    note = Column(Text, nullable=True)

    # ── 客戶條件（2026-08-20 補回，見 2026-08-20-billing-phase2b-customer-terms.md）──
    payment_method = Column(String, nullable=False, default="monthly_transfer", server_default="monthly_transfer")
    statement_day = Column(Integer, nullable=False, default=1, server_default="1")  # 1-28，避開月底不存在的日期
    # 客戶級自訂月費：設了就覆蓋該客戶所有相機的方案月費。
    # NULL = 未設定（用方案月費）；0 是有效值（談成免費），兩者語意不同。
    custom_monthly_fee = Column(Integer, nullable=True)
    # 分潤不進發票（設計決定：另列應付），只用於 /admin/commissions 報表。
    # 兩個數值欄位各自獨立驗證上限，避免共用欄位時 percent 的「不超過 100%」
    # 保護把 fixed 綁死在 10000 元；只讀 commission_type 對應的那一個。
    commission_type = Column(String, nullable=True)          # percent | fixed
    commission_percent_bps = Column(Integer, nullable=True)  # 萬分比：1250 = 12.5%
    commission_fixed_amount = Column(Integer, nullable=True) # TWD 整數


class BillingSubscription(Base):
    """一台相機一份訂閱。

    同一台相機同時間只能有一份「生效中」的訂閱，否則會重複計費。
    router 端原本只靠 SELECT-then-INSERT 防重複，並發下擋不住，
    所以這裡也加一道「部分唯一索引」（比照 BillingInvoice 的作法）當最後防線。
    """
    __tablename__ = "billing_subscriptions"
    __table_args__ = (
        Index(
            "uq_billing_subscription_camera_active",
            "camera_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    camera_id = Column(Integer, nullable=False, index=True)
    customer_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    plan_id = Column(Integer, ForeignKey("billing_plans.id"), nullable=False)
    status = Column(String, nullable=False, default="active")  # active | paused | cancelled
    started_at = Column(DateTime, default=datetime.utcnow)
    cancelled_at = Column(DateTime, nullable=True)

    # NAS 目錄名（= Camera Backend 的 device_serial_id）。
    # 由用量採集在首次需要時解析並快取，避免每日為每台相機重複打 Camera Backend。
    camera_serial = Column(String, nullable=True)


class BillingInvoice(Base):
    """月結發票。(customer_id, period) 唯一是產生作業冪等性的根據。

    這裡刻意用「部分唯一索引」而非一般 UniqueConstraint：作廢(void)的發票
    不算數，讓該客戶＋期別可以重新開立；但同一時間絕不能有兩張「非作廢」
    的發票佔用同一期別，否則會重複收費。
    """
    __tablename__ = "billing_invoices"
    __table_args__ = (
        Index(
            "uq_billing_invoice_customer_period_active",
            "customer_id", "period",
            unique=True,
            postgresql_where=text("status != 'void'"),
            sqlite_where=text("status != 'void'"),
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    period = Column(String, nullable=False, index=True)  # YYYY-MM
    total = Column(Integer, nullable=False, default=0)
    status = Column(String, nullable=False, default="unpaid")  # unpaid | paid | void
    issued_at = Column(DateTime, default=datetime.utcnow)
    paid_at = Column(DateTime, nullable=True)
    paid_note = Column(Text, nullable=True)


class BillingInvoiceLine(Base):
    """發票明細。camera_name/plan_name/amount 是開立當下的快照，不可回頭變動。"""
    __tablename__ = "billing_invoice_lines"

    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(Integer, ForeignKey("billing_invoices.id"), nullable=False, index=True)
    subscription_id = Column(Integer, nullable=True)
    camera_id = Column(Integer, nullable=False)
    camera_name = Column(String, nullable=True)
    plan_name = Column(String, nullable=True)
    amount = Column(Integer, nullable=False, default=0)


class BillingUsageDaily(Base):
    """每日用量。存日粒度而非累計值，讓採集失敗後可安全補跑（upsert 覆蓋同一天）。"""
    __tablename__ = "billing_usage_daily"
    __table_args__ = (UniqueConstraint("camera_id", "date", name="uq_billing_usage_camera_date"),)

    id = Column(Integer, primary_key=True, index=True)
    camera_id = Column(Integer, nullable=False, index=True)
    date = Column(String, nullable=False, index=True)  # YYYY-MM-DD（台北時區）
    timelapse_secs = Column(Integer, nullable=False, default=0)
    storage_gb = Column(Float, nullable=False, default=0)
    collected_at = Column(DateTime, default=datetime.utcnow)
