from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import time, logging

from config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Symotus Auth Service",
    description="權限管理服務",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
            logger.info("DB connected and tables created!")
            break
        except Exception as e:
            logger.warning(f"DB not ready: {e}")
            if i < max_retries - 1:
                time.sleep(5)
            else:
                logger.error("Failed to connect to DB after all retries")
                raise

from routers import auth, invites, users, support, admin, jobs, cameras, line_webhook, invitations
app.include_router(auth.router)
app.include_router(invites.router)
app.include_router(users.router)
app.include_router(support.router)
app.include_router(admin.router)
app.include_router(jobs.router)
app.include_router(cameras.router)
app.include_router(line_webhook.router)
app.include_router(invitations.router)

@app.get("/health")
def health():
    return {"status": "ok", "service": "symotus-auth"}


if __name__ == "__main__":
    import uvicorn, os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
