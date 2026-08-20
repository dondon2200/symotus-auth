from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import time, logging, asyncio

from config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
                ]:
                    try:
                        conn.execute(text(f"ALTER TABLE timelapse_jobs ADD COLUMN IF NOT EXISTS {col} {typ}"))
                        conn.commit()
                    except Exception:
                        conn.rollback()
            # 在 users 表加 camera_email（若尚未存在）
            from sqlalchemy import text
            with engine.connect() as conn:
                try:
                    conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS camera_email TEXT"))
                    conn.commit()
                except Exception:
                    conn.rollback()
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
                    except Exception:
                        conn.rollback()

            # 補上 camera_access.permission_level
            with engine.connect() as conn:
                try:
                    conn.execute(text("ALTER TABLE camera_access ADD COLUMN IF NOT EXISTS permission_level VARCHAR DEFAULT 'photos_stream' NOT NULL"))
                    conn.commit()
                except Exception:
                    conn.rollback()
            with engine.connect() as conn:
                try:
                    conn.execute(text("ALTER TABLE camera_access ADD COLUMN IF NOT EXISTS notify_on_online BOOLEAN DEFAULT TRUE NOT NULL"))
                    conn.commit()
                except Exception:
                    conn.rollback()
            with engine.connect() as conn:
                try:
                    conn.execute(text("ALTER TABLE camera_access ADD COLUMN IF NOT EXISTS invitation_id INTEGER"))
                    conn.commit()
                except Exception:
                    conn.rollback()

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
                    except Exception:
                        conn.rollback()

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
                    except Exception:
                        conn.rollback()

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
                            name="標準方案",
                            description="預設方案，月費為暫定值，正式定價確定後於後台調整",
                            monthly_fee=9999,
                            timelapse_quota_secs=0,
                            storage_quota_gb=0,
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
app.include_router(billing.router)  # 必須在 cameras 前（避免 /{camera_id} catch-all 攔截）
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
