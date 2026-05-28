from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from database import engine
from models import Base
from routers import auth, invites, users, support, admin
from config import settings

# 建立所有資料表
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Symotus Auth Service",
    description="權限管理服務 — 負責登入、角色、邀請、相機存取授權",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_URL, "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(invites.router)
app.include_router(users.router)
app.include_router(support.router)
app.include_router(admin.router)

@app.get("/health")
def health():
    return {"status": "ok", "service": "symotus-auth"}
