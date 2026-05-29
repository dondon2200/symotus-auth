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
    allow_origins=[settings.FRONTEND_URL, "http://localhost:3000"],
    allow_credentials=True,
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
            logger.info("DB connected and tables created!")
            break
        except Exception as e:
            logger.warning(f"DB not ready: {e}")
            if i < max_retries - 1:
                time.sleep(5)
            else:
                logger.error("Failed to connect to DB after all retries")
                raise

from routers import auth, invites, users, support, admin, jobs
app.include_router(auth.router)
app.include_router(invites.router)
app.include_router(users.router)
app.include_router(support.router)
app.include_router(admin.router)
app.include_router(jobs.router)

@app.get("/health")
def health():
    return {"status": "ok", "service": "symotus-auth"}


if __name__ == "__main__":
    import uvicorn, os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
