from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import time, logging, asyncio

from config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_USAGE_TASK = None  # 模組層級，避免 asyncio.create_task 的回傳值被 GC 回收後任務無聲消失

app = FastAPI(
    title="Symotus Auth Service",
    description="權限管理服務",
    version="1.0.0",
)

# F-8：CORS 收斂為已知前端來源（前端與 /auth-api 同源，正常流量不依賴 CORS）
_ALLOWED_ORIGINS = list({
    settings.FRONTEND_URL,
    "https://user.symotus.com",
    "https://admin.symotus.com",
})

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup():
    """啟動時等 DB 準備好再建表"""
    from database import engine
    from models import Base
    
    max_retries = 10
    for i in range(max_retries):
        try:
            logger.info(f"Connecting to DB (attempt {i+1}/{max_retries})...")
            Base.metadata.create_all(bind=engine)
            # 補上後來加的欄位（舊 DB 可能沒有）
            from sqlalchemy import text
            with engine.connect() as conn:
                for col, typ in [
                    ("video_url", "TEXT"),
                    ("error_message", "TEXT"),
                    ("image_count", "INTEGER"),
                    ("processing_time_secs", "TEXT"),
                    ("completed_at", "TIMESTAMP"),
                ]:
                    try:
                        conn.execute(text(f"ALTER TABLE timelapse_jobs ADD COLUMN IF NOT EXISTS {col} {typ}"))
                        conn.commit()
                    except Exception as e:
                        conn.rollback()
                        logger.warning(f"schema migration 補欄位失敗（略過，可能是權限不足或鎖表）：{e}")
            # 在 users 表加 camera_email（若尚未存在）
            from sqlalchemy import text
            with engine.connect() as conn:
                try:
                    conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS camera_email TEXT"))
                    conn.commit()
                except Exception as e:
                    conn.rollback()
                    logger.warning(f"schema migration 補欄位失敗（略過，可能是權限不足或鎖表）：{e}")
            # 確保 camera_invitations table 存在（新功能）
            with engine.connect() as conn:
                try:
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS camera_invitations (
                            id SERIAL PRIMARY KEY,
                            token VARCHAR UNIQUE NOT NULL,
                            inviter_id INTEGER REFERENCES users(id),
                            camera_id INTEGER NOT NULL,
                            camera_name VARCHAR,
                            note TEXT,
                            permission_level VARCHAR DEFAULT 'photos_stream',
                            status VARCHAR DEFAULT 'pending',
                            invitee_id INTEGER REFERENCES users(id),
                            expires_at TIMESTAMP,
                            responded_at TIMESTAMP,
                            created_at TIMESTAMP DEFAULT NOW()
                        )
                    """))
                    conn.commit()
                except Exception as e:
                    conn.rollback()
                    logger.warning(f"camera_invitations migration: {e}")

            # 補上 camera_invitations 後加的欄位
            with engine.connect() as conn:
                for col, typ, default in [
                    ("token", "VARCHAR", None),
                    ("permission_level", "VARCHAR", "'photos_stream'"),
                    ("invitee_id", "INTEGER", None),
                    ("expires_at", "TIMESTAMP", None),
                    ("responded_at", "TIMESTAMP", None),
                    ("is_public", "BOOLEAN", "FALSE"),
                ]:
                    try:
                        if default:
                            conn.execute(text(f"ALTER TABLE camera_invitations ADD COLUMN IF NOT EXISTS {col} {typ} DEFAULT {default}"))
                        else:
                            conn.execute(text(f"ALTER TABLE camera_invitations ADD COLUMN IF NOT EXISTS {col} {typ}"))
                        conn.commit()
                    except Exception as e:
                        conn.rollback()
                        logger.warning(f"schema migration 補欄位失敗（略過，可能是權限不足或鎖表）：{e}")

            # 補上 camera_access.permission_level
            with engine.connect() as conn:
                try:
                    conn.execute(text("ALTER TABLE camera_access ADD COLUMN IF NOT EXISTS permission_level VARCHAR DEFAULT 'photos_stream' NOT NULL"))
                    conn.commit()
                except Exception as e:
                    conn.rollback()
                    logger.warning(f"schema migration 補欄位失敗（略過，可能是權限不足或鎖表）：{e}")
            with engine.connect() as conn:
                try:
                    conn.execute(text("ALTER TABLE camera_access ADD COLUMN IF NOT EXISTS notify_on_online BOOLEAN DEFAULT TRUE NOT NULL"))
                    conn.commit()
                except Exception as e:
                    conn.rollback()
                    logger.warning(f"schema migration 補欄位失敗（略過，可能是權限不足或鎖表）：{e}")
            with engine.connect() as conn:
                try:
                    conn.execute(text("ALTER TABLE camera_access ADD COLUMN IF NOT EXISTS invitation_id INTEGER"))
                    conn.commit()
                except Exception as e:
                    conn.rollback()
                    logger.warning(f"schema migration 補欄位失敗（略過，可能是權限不足或鎖表）：{e}")

            # 補上 gdrive_jobs 新流程欄位（OAuth + Picker）並放寬 folder_url
            with engine.connect() as conn:
                for stmt in [
                    "ALTER TABLE gdrive_jobs ADD COLUMN IF NOT EXISTS folder_id VARCHAR",
                    "ALTER TABLE gdrive_jobs ADD COLUMN IF NOT EXISTS folder_name VARCHAR",
                    "ALTER TABLE gdrive_jobs ADD COLUMN IF NOT EXISTS google_refresh_token VARCHAR",
                    "ALTER TABLE gdrive_jobs ADD COLUMN IF NOT EXISTS job_params TEXT",
                    "ALTER TABLE gdrive_jobs ALTER COLUMN folder_url DROP NOT NULL",
                    """CREATE TABLE IF NOT EXISTS google_drive_credentials (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
                        refresh_token VARCHAR NOT NULL,
                        google_email VARCHAR,
                        scope VARCHAR,
                        created_at TIMESTAMP DEFAULT NOW(),
                        updated_at TIMESTAMP DEFAULT NOW()
                    )""",
                ]:
                    try:
                        conn.execute(text(stmt))
                        conn.commit()
                    except Exception as e:
                        conn.rollback()
                        logger.warning(f"schema migration 補欄位失敗（略過，可能是權限不足或鎖表）：{e}")

            # 補上 invite_tokens 的角色 + 預綁 Camera Backend 帳號欄位
            with engine.connect() as conn:
                for stmt in [
                    "ALTER TABLE invite_tokens ADD COLUMN IF NOT EXISTS intended_role VARCHAR NOT NULL DEFAULT 'end_user'",
                    "ALTER TABLE invite_tokens ADD COLUMN IF NOT EXISTS camera_email VARCHAR",
                    "ALTER TABLE invite_tokens ADD COLUMN IF NOT EXISTS camera_user_id INTEGER",
                ]:
                    try:
                        conn.execute(text(stmt))
                        conn.commit()
                    except Exception as e:
                        conn.rollback()
                        logger.warning(f"schema migration 補欄位失敗（略過，可能是權限不足或鎖表）：{e}")

            # 補建 billing 部分唯一索引（給既有環境用）。
            # create_all() 只會對「尚不存在」的表建索引，不會替既有表補建索引；
            # 這個環境目前還沒有 billing_* 表，create_all 會建好一切，這裡是保險。
            # 若既有資料已有重複列（例如手動塞資料造成同客戶同期別兩張非作廢發票），
            # 建索引會失敗；失敗只記 log、rollback，不讓服務啟動失敗，需要人工清理重複資料。
            with engine.connect() as conn:
                for stmt in [
                    """CREATE UNIQUE INDEX IF NOT EXISTS uq_billing_invoice_customer_period_active
                        ON billing_invoices (customer_id, period)
                        WHERE (status != 'void')""",
                    """CREATE UNIQUE INDEX IF NOT EXISTS uq_billing_subscription_camera_active
                        ON billing_subscriptions (camera_id)
                        WHERE (status = 'active')""",
                ]:
                    try:
                        conn.execute(text(stmt))
                        conn.commit()
                    except Exception as e:
                        conn.rollback()
                        logger.warning(f"billing 部分唯一索引補建失敗，需人工清理重複資料：{e}")

            # 補上 billing_customers 的客戶條件欄位（既有表 create_all 不會補）
            # payment_method / statement_day 在 models.py 是 nullable=False，
            # 原本只用 ADD COLUMN ... DEFAULT 補欄位、沒有補 NOT NULL 約束，
            # 造成既有 DB 的欄位定義與 models.py 不一致。分三步各自補齊，
            # 每一步各自 try/except/rollback，任一步在既有環境失敗（例如
            # 欄位已存在但仍有 NULL 值）只記警告，不擋啟動。
            NOT_NULL_COLS = [
                ("payment_method", "TEXT", "'monthly_transfer'"),
                ("statement_day", "INTEGER", "1"),
            ]
            for col, typ, default in NOT_NULL_COLS:
                with engine.connect() as conn:
                    try:
                        conn.execute(text(
                            f"ALTER TABLE billing_customers ADD COLUMN IF NOT EXISTS {col} {typ} DEFAULT {default}"
                        ))
                        conn.commit()
                    except Exception as e:
                        conn.rollback()
                        logger.warning(f"billing_customers 補欄位 {col}（ADD COLUMN）失敗：{e}")
                with engine.connect() as conn:
                    try:
                        conn.execute(text(
                            f"UPDATE billing_customers SET {col} = {default} WHERE {col} IS NULL"
                        ))
                        conn.commit()
                    except Exception as e:
                        conn.rollback()
                        logger.warning(f"billing_customers 補欄位 {col}（UPDATE NULL）失敗：{e}")
                with engine.connect() as conn:
                    try:
                        conn.execute(text(
                            f"ALTER TABLE billing_customers ALTER COLUMN {col} SET NOT NULL"
                        ))
                        conn.commit()
                    except Exception as e:
                        conn.rollback()
                        logger.warning(f"billing_customers 補欄位 {col}（SET NOT NULL）失敗：{e}")

            # custom_monthly_fee / commission_type / commission_percent_bps /
            # commission_fixed_amount 在 models.py 皆為 nullable=True，維持原本
            # 單純 ADD COLUMN 即可。
            with engine.connect() as conn:
                for col, typ in [
                    ("custom_monthly_fee", "INTEGER"),
                    ("commission_type", "TEXT"),
                    ("commission_percent_bps", "INTEGER"),
                    ("commission_fixed_amount", "INTEGER"),
                ]:
                    try:
                        conn.execute(text(f"ALTER TABLE billing_customers ADD COLUMN IF NOT EXISTS {col} {typ}"))
                        conn.commit()
                    except Exception as e:
                        conn.rollback()
                        logger.warning(f"billing_customers 補欄位 {col} 失敗：{e}")

            # billing_subscriptions.camera_serial：NAS 用量採集快取欄位。
            # 這張表已在正式庫存在，create_all 不會補欄位，需比照上面手動 ALTER。
            with engine.connect() as conn:
                try:
                    conn.execute(text("ALTER TABLE billing_subscriptions ADD COLUMN IF NOT EXISTS camera_serial TEXT"))
                    conn.commit()
                except Exception as e:
                    conn.rollback()
                    logger.warning(f"billing_subscriptions 補欄位 camera_serial 失敗：{e}")

            # 自我檢查：確認上面的欄位真的都補上了。計費模組非核心功能
            # （auth service 同時代理所有相機 CRUD），欄位缺失不該擋住服務啟動，
            # 但要在 log 大聲示警，讓維運人員能人工介入，而不是靜默讓計費 API 500。
            with engine.connect() as conn:
                try:
                    conn.execute(text(
                        "SELECT payment_method, statement_day, custom_monthly_fee, "
                        "commission_type, commission_percent_bps, commission_fixed_amount "
                        "FROM billing_customers LIMIT 1"
                    ))
                except Exception as e:
                    logger.error(
                        "billing_customers 缺少欄位，計費 API 將失效，請人工檢查 migration：" + str(e)
                    )

            with engine.connect() as conn:
                try:
                    conn.execute(text("SELECT camera_serial FROM billing_subscriptions LIMIT 1"))
                except Exception as e:
                    logger.error(
                        "billing_subscriptions 缺少 camera_serial 欄位，NAS 儲存用量採集將失效，"
                        "請人工檢查 migration：" + str(e)
                    )

            # timelapse_jobs.completed_at：這張表在正式庫已存在且有資料，
            # create_all 不會補欄位。若 ALTER 失敗，SQLAlchemy 之後對 TimelapsJob
            # 的每次查詢都會 SELECT 一個不存在的欄位 → /jobs 相關功能全部 500，
            # 所以同樣要自我檢查、大聲示警。
            with engine.connect() as conn:
                try:
                    conn.execute(text("SELECT completed_at FROM timelapse_jobs LIMIT 1"))
                except Exception as e:
                    logger.error(
                        "timelapse_jobs 缺少 completed_at 欄位，/jobs 相關功能將失效，"
                        "請人工檢查 migration：" + str(e)
                    )

            logger.info("DB connected and tables created!")
            # 種子功能權限政策（缺列才補，不覆蓋既有調整）
            try:
                from database import SessionLocal
                from policies import seed_policies
                with SessionLocal() as _s:
                    seed_policies(_s)
            except Exception as e:
                logger.warning(f"seed_policies: {e}")
            # billing 種子方案：全新環境建一個預設方案，避免後台空白無從下手。
            # 已有任何方案就不動——不覆寫營運中的資料。
            try:
                from models import BillingPlan
                from database import SessionLocal
                s = SessionLocal()
                try:
                    if s.query(BillingPlan).count() == 0:
                        s.add(BillingPlan(
                            name="（範本）標準方案",
                            description="佔位範本，月費 9999 為暫定值尚未定案。"
                                        "正式定價確定後，請在後台編輯價格並啟用。"
                                        "啟用前不可指派給任何相機，以免產生真實的 9999/月發票。",
                            monthly_fee=9999,
                            timelapse_quota_secs=0,
                            storage_quota_gb=0,
                            is_active=False,
                        ))
                        s.commit()
                        logger.info("billing: 已建立預設方案")
                except Exception as e:
                    s.rollback()
                    logger.warning(f"billing 種子方案建立失敗（不影響啟動）：{e}")
                finally:
                    s.close()
            except Exception as e:
                logger.warning(f"billing 種子方案初始化失敗（不影響啟動）：{e}")
            # 回收上一個進程留下的 GDrive 下載孤兒（部署／SIGKILL 會讓它們永遠停在
            # downloading）。必須在開放流量前做，否則使用者會看到永遠不動的進度條。
            try:
                from routers.jobs import reap_orphaned_gdrive_jobs
                reap_orphaned_gdrive_jobs()
            except Exception as e:
                logger.warning(f"reap_orphaned_gdrive_jobs: {e}")
            # 啟動相機開機 LINE 推播背景工作
            from services.camera_notifier import start_camera_notifier
            asyncio.create_task(start_camera_notifier())
            # 啟動計費用量每日採集背景工作
            from services.billing_usage import start_usage_collector
            global _USAGE_TASK
            _USAGE_TASK = asyncio.create_task(start_usage_collector())
            break
        except Exception as e:
            logger.warning(f"DB not ready: {e}")
            if i < max_retries - 1:
                time.sleep(5)
            else:
                logger.error("Failed to connect to DB after all retries")
                raise

@app.on_event("shutdown")
async def shutdown():
    """優雅關機：把進行中的 GDrive 下載標記為 interrupted，讓它們可被續傳。

    Docker 送 SIGTERM 後預設有 10 秒寬限，足夠寫一筆 DB。走不到這裡的情況（SIGKILL、
    斷電）由啟動時的 reap_orphaned_gdrive_jobs() 兜底。
    """
    try:
        from routers.jobs import shutdown_gdrive_jobs
        await shutdown_gdrive_jobs()
    except Exception as e:
        logger.warning(f"shutdown_gdrive_jobs: {e}")


from routers import auth, invites, users, support, admin, jobs, cameras, line_webhook, invitations, public_camera, billing
app.include_router(auth.router)
app.include_router(invites.router)
app.include_router(users.router)
app.include_router(support.router)
app.include_router(admin.router)
app.include_router(jobs.router)
app.include_router(billing.router)  # 前綴 /billing 與 cameras 的 /cameras 不相交，順序對兩者無影響；
                                     # 真正要留意的是 billing router 內部 /invoices/my 需先於
                                     # /invoices/{invoice_id}，見 tests/test_billing_route_order.py
app.include_router(public_camera.router)  # 必須在 cameras 前（避免 /{camera_id}/{path} catch-all 攔截）
app.include_router(cameras.router)
app.include_router(line_webhook.router)
app.include_router(invitations.router)

from fastapi import Request
from fastapi.responses import JSONResponse

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    import traceback
    logger.error(f"Unhandled exception: {exc}\n{traceback.format_exc()}")
    # F-10：不外洩內部細節（路徑/SQL/上游回應）；CORS 交由中介層處理
    return JSONResponse(
        status_code=500,
        content={"detail": "伺服器發生錯誤，請稍後再試"},
    )

@app.get("/health")
def health():
    return {"status": "ok", "service": "symotus-auth"}


from fastapi import Depends
from sqlalchemy.orm import Session as _Session
from database import get_db
from auth import get_current_user


@app.get("/policies")
def public_policies(db: _Session = Depends(get_db), _user=Depends(get_current_user)):
    """功能權限政策（任何登入者可讀，供前端 UI gating；安全判斷仍在後端各 gate）"""
    from policies import get_policies, FEATURE_DEFAULTS
    pols = get_policies(db)
    order = [k for k, _, _ in FEATURE_DEFAULTS]
    return [{"feature_key": k, "min_level": pols[k]["min_level"], "enabled": pols[k]["enabled"]}
            for k in order if k in pols]


@app.get("/version")
def version():
    """回傳後端版本碼（CI build 時蓋的 YYYYMMDDHHMM，未設為 'dev'），供前端啟動時比對是否更新"""
    return {"version": settings.BUILD_VERSION, "service": "symotus-auth"}


if __name__ == "__main__":
    import uvicorn, os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
