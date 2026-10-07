from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, Date, ForeignKey, ARRAY, UniqueConstraint, Float, Index, LargeBinary, text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship, backref, declarative_base
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
    role = Column(String, nullable=False, default="end_user")  # symotus_admin | reseller | end_user
    is_active = Column(Boolean, default=True)
    # 工程模式：前端顯示工程測試用的隱藏設定（例：拍照星期 DAY0–DAY6、OSD 字幕）。
    # 只影響畫面；由 symotus_admin 在帳號管理切換，寫進 JWT 與 /auth/me。
    engineering_mode = Column(Boolean, nullable=False, default=False, server_default="false")
    reseller_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # end_user -> reseller
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Camera Backend 帳號對應
    camera_email = Column(String, nullable=True)      # Camera Backend 對應帳號 email
    camera_user_id = Column(Integer, nullable=True)   # Camera Backend 的真實 user_id

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    camera_accesses = relationship("CameraAccess", foreign_keys="CameraAccess.user_id", back_populates="user")
    granted_accesses = relationship("CameraAccess", foreign_keys="CameraAccess.granted_by", back_populates="granter")
    sent_invites = relationship("InviteToken", foreign_keys="InviteToken.reseller_id", back_populates="reseller")


class UserLineAccount(Base):
    """Symotus 帳號 ↔ LINE 帳號多對多：一帳號可綁多個 LINE，一個 LINE 也可綁多個帳號。
    is_active：同一 line_user_id 的多列中最多一列 True（程式維護，非 DB 約束），
    代表 LINE AI 助理的「作用中」帳號；通知不看此欄位（推播給所有綁定帳號的聯集）。"""
    __tablename__ = "user_line_accounts"
    __table_args__ = (UniqueConstraint("user_id", "line_user_id",
                                       name="uq_user_line_accounts_user_line"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    line_user_id = Column(String, nullable=False, index=True)
    display_name = Column(String, nullable=True)
    picture_url = Column(String, nullable=True)
    is_active = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # order_by：id 遞增，讓 User.line_accounts[0] 這類存取有穩定、可預期的順序
    # （原本是無序 backref，同一 line_user_id 底下哪列排第一是不確定的）。
    user = relationship("User", backref=backref("line_accounts", order_by="UserLineAccount.id"))


class LineBindCode(Base):
    """LINE 官方帳號綁定碼：網頁產生 6 位數字，使用者在官方帳號聊天輸入完成綁定。
    單次使用（used_at）、10 分鐘效期（expires_at）；產新碼時作廢同 user 舊碼。"""
    __tablename__ = "line_bind_codes"

    id = Column(Integer, primary_key=True)
    code = Column(String(6), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User")


class LineBindSession(Base):
    """LINE Login 一鍵綁定的一次性 session。

    身分留在後端：手機掃描桌機顯示的 QR 時，不需要在手機上重新登入平台。
    sid 等同憑證（持有者可把自己的 LINE 綁進該帳號），故 5 分鐘效期 + 單次使用。
    不用記憶體 dict：綁定跨裝置、跨數分鐘，auth 容器有 mem_limit 且部署會重建，
    state 一掉使用者就白掃一次 QR。"""
    __tablename__ = "line_bind_sessions"

    id = Column(Integer, primary_key=True)
    sid = Column(String, nullable=False, unique=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User")


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
    # 任務完成時間（naive UTC），取自 Spark 回報。舊資料為 NULL。
    # （2026-08-20 版計費曾以此歸屬用量；計費 v2 已不計量，欄位保留供任務頁顯示。）
    completed_at = Column(DateTime, nullable=True)
    # Spark 回報的產出影片實際長度（秒）。舊資料為 NULL。
    # 不可用 image_count/fps 推算——image_count 是「可用的來源照片張數」，
    # Spark 依 target_duration_secs 抽樣，實測兩者差 2～6 倍且倍率不固定。
    video_duration_secs = Column(Float, nullable=True)

    # 切日條件 collect_timelapse_secs 用 COALESCE(completed_at, created_at) 過濾，
    # 普通單欄索引吃不到，要用函式（expression）索引。目前表只有 22 列，感覺不到
    # 差別；這裡是為相機/任務量成長預先準備，不是在解決現在的效能問題。
    # PostgreSQL 與 SQLite（3.9+）都支援 expression index，SQLAlchemy 對兩者都能
    # 產生對應的 CREATE INDEX（測過 sqlite 可以建，不像 BillingSubscription/
    # BillingInvoice 那兩個部分唯一索引需要 postgresql_where/sqlite_where 分開處理
    # ——那兩個是「帶 WHERE 條件」的索引，sqlite 的 WHERE 語法與 PG 需要分開給；
    # 這裡單純是 expression 本身，不帶 WHERE，兩種方言共用同一個宣告即可）。
    # 既有正式庫（表已存在）另外在 main.py 的啟動 migration 補一次
    # CREATE INDEX IF NOT EXISTS，create_all 不會替既有表補索引。
    __table_args__ = (
        Index(
            "ix_timelapse_jobs_effective_time",
            func.coalesce(completed_at, created_at),
        ),
    )
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

    # 自助建帳（spec 2026-09-02）
    invitee_email = Column(String, nullable=True)                                   # 指定對象；有值則建帳與接受都須為此 email
    signup_limit = Column(Integer, nullable=True)                                   # 可自助建帳人數上限；NULL 視為 10
    signup_count = Column(Integer, default=0, nullable=False, server_default="0")   # 已自助建帳人數


# ── Billing 計費模組 v2：按相機訂閱 ─────────────────────────────────
# 設計見 symotus-frontend/docs/superpowers/specs/2026-10-07-camera-subscription-billing-design.md
# 金額一律整數 TWD；日期（帳單日、截止日、付款日）一律存台北日期（Date），時間戳存 naive UTC。
# 2026-08-20 版的 billing_plans / billing_customers / billing_subscriptions / billing_invoices /
# billing_invoice_lines / billing_usage_daily 表仍留在正式庫、不再讀寫；確認 v2 穩定後另開工單 DROP。

class BillingPlanV2(Base):
    """方案 = 合約期 × 繳費週期 × 每期金額 的一種組合。月約年繳不允許（spec D2）。"""
    __tablename__ = "billing_plans_v2"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    term = Column(String, nullable=False)    # monthly | annual
    cycle = Column(String, nullable=False)   # monthly | yearly
    price = Column(Integer, nullable=False)  # 每期金額 TWD，0 合法（免費）
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class BillingSubscriptionV2(Base):
    """一台相機一份訂閱。plan_name/term/cycle/price 建立時快照，方案改價不影響。

    同一台相機同時最多一份 scheduled/active/suspended：router 端先查再寫擋不住並發，
    部分唯一索引是最後防線。"""
    __tablename__ = "billing_subscriptions_v2"
    __table_args__ = (
        Index(
            "uq_billing_sub_v2_camera_open",
            "camera_id",
            unique=True,
            postgresql_where=text("status IN ('scheduled', 'active', 'suspended')"),
            sqlite_where=text("status IN ('scheduled', 'active', 'suspended')"),
        ),
        Index("ix_billing_sub_v2_camera_status", "camera_id", "status"),
    )

    id = Column(Integer, primary_key=True, index=True)
    camera_id = Column(Integer, nullable=False, index=True)
    camera_name = Column(String, nullable=True)     # 建立時快照，帳單沿用
    camera_serial = Column(String, nullable=True)   # NAS 目錄名；鎖定時擋 /cameras/nas/image 用
    customer_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)  # 付款人（reseller）
    plan_id = Column(Integer, ForeignKey("billing_plans_v2.id"), nullable=False)
    plan_name = Column(String, nullable=False)
    term = Column(String, nullable=False)
    cycle = Column(String, nullable=False)
    price = Column(Integer, nullable=False)
    start_date = Column(Date, nullable=False)       # 第一期帳單日（台北）
    anchor_day = Column(Integer, nullable=False)    # = start_date.day，1–31
    term_start = Column(Date, nullable=False)       # 目前合約期起點；年約續約時往後推 12 個月
    auto_renew = Column(Boolean, nullable=False, default=True)
    cancel_at = Column(Date, nullable=True)         # 「到期不續約」的生效日（台北）；NULL = 未設
    status = Column(String, nullable=False, default="active")  # scheduled | active | suspended | ended
    suspended_at = Column(DateTime, nullable=True)
    ended_at = Column(DateTime, nullable=True)
    end_reason = Column(String, nullable=True)      # not_renewed | terminated | non_payment
    lock_released_at = Column(DateTime, nullable=True)
    note = Column(Text, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class BillingBill(Base):
    """一份訂閱的一期應繳款。

    UNIQUE(subscription_id, period_start) 不排除作廢：帳單由排程自動補開，
    若作廢後可重開，排程隔天就會把它補回來（spec §6.4）。"""
    __tablename__ = "billing_bills"
    __table_args__ = (
        UniqueConstraint("subscription_id", "period_start", name="uq_billing_bill_sub_period"),
        Index("ix_billing_bill_customer_status", "customer_id", "status"),
        Index("ix_billing_bill_status_due", "status", "due_date"),
    )

    id = Column(Integer, primary_key=True, index=True)
    subscription_id = Column(Integer, ForeignKey("billing_subscriptions_v2.id"), nullable=False, index=True)
    camera_id = Column(Integer, nullable=False, index=True)
    customer_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    camera_name = Column(String, nullable=True)
    plan_name = Column(String, nullable=True)
    cycle = Column(String, nullable=False)
    period_start = Column(Date, nullable=False)     # = 帳單日
    period_end = Column(Date, nullable=False)       # 下一期帳單日（半開區間）
    due_date = Column(Date, nullable=False)
    amount = Column(Integer, nullable=False)
    status = Column(String, nullable=False, default="unpaid")  # unpaid | paid | void
    paid_on = Column(Date, nullable=True)           # 客戶實際付款日
    paid_at = Column(DateTime, nullable=True)       # admin 確認時間
    paid_note = Column(Text, nullable=True)
    paid_via_report_id = Column(Integer, nullable=True)
    pending_report_id = Column(Integer, nullable=True, index=True)  # 目前所在的 pending 回報
    voided_at = Column(DateTime, nullable=True)
    void_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class BillingPaymentReport(Base):
    """reseller 送出的「我已付款」。回報本身不改帳單狀態，admin 確認後才算已繳。"""
    __tablename__ = "billing_payment_reports"
    __table_args__ = (
        Index("ix_billing_report_status_created", "status", "created_at"),
        Index("ix_billing_report_customer_status", "customer_id", "status"),
    )

    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    paid_on = Column(Date, nullable=False)
    amount = Column(Integer, nullable=False)
    method = Column(String, nullable=False)          # transfer | cash | other
    account_last5 = Column(String(5), nullable=True)
    note = Column(Text, nullable=True)
    status = Column(String, nullable=False, default="pending")  # pending | confirmed | rejected | withdrawn
    reviewed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    review_note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class BillingPaymentReportBill(Base):
    """回報當下勾了哪些帳單（歷史，不刪）。confirmed 在審核時逐張寫入。"""
    __tablename__ = "billing_payment_report_bills"

    report_id = Column(Integer, ForeignKey("billing_payment_reports.id"), primary_key=True)
    bill_id = Column(Integer, ForeignKey("billing_bills.id"), primary_key=True)
    amount_snapshot = Column(Integer, nullable=False)
    confirmed = Column(Boolean, nullable=True)


class BillingPaymentReceipt(Base):
    """收據檔案。存 DB 而不是檔案系統：auth 容器沒有自己的持久化 volume，量也小（spec §11.1）。"""
    __tablename__ = "billing_payment_receipts"

    report_id = Column(Integer, ForeignKey("billing_payment_reports.id"), primary_key=True)
    content_type = Column(String, nullable=False)
    data = Column(LargeBinary, nullable=False)
    sha256 = Column(String(64), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class BillingSetting(Base):
    """計費設定（key/value）。目前只有 payment_instructions。"""
    __tablename__ = "billing_settings"

    key = Column(String, primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
